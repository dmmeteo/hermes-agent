"""Packet 3, slice 3A — typed ACP event projection, driven through AIAgent.

Every test spawns a REAL fake adapter subprocess over REAL stdio. The failure
modes this projection exists to prevent do not reproduce against mocks:

* a raw protocol payload (tool ids, ``rawOutput``, replayed user text, plan
  entries) reaching the assistant response or the streaming surface,
* an unscrubbed credential reaching a display callback,
* context occupancy (``usage_update.used``) being summed into per-turn token
  counts, which would inflate session accounting by orders of magnitude.

The projected surfaces are the ones Hermes' native tool loop already fires
(``agent/tool_executor.py``): ``tool_progress_callback``,
``tool_start_callback``, ``tool_complete_callback``, ``stream_delta_callback``,
``reasoning_callback``. Nothing new is invented for this runtime.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

import run_agent
from tests.agent import _fake_claude_acp_adapter as fake

_FAKE_ADAPTER = os.path.join(os.path.dirname(__file__), "_fake_claude_acp_adapter.py")


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


class _Surfaces:
    """Captures every Hermes display surface the projection is allowed to use."""

    def __init__(self, agent):
        self.progress: list[tuple] = []
        self.starts: list[tuple] = []
        self.completes: list[tuple] = []
        self.deltas: list[str] = []
        self.reasoning: list[str] = []

        agent.tool_progress_callback = (
            lambda event, name=None, preview=None, args=None, **kw:
            self.progress.append((event, name, preview, args, kw))
        )
        agent.tool_start_callback = (
            lambda call_id, name, args: self.starts.append((call_id, name, args))
        )
        agent.tool_complete_callback = (
            lambda call_id, name, args, result:
            self.completes.append((call_id, name, args, result))
        )
        agent.stream_delta_callback = self.deltas.append
        agent.reasoning_callback = self.reasoning.append

    # -- helpers -----------------------------------------------------------

    def events(self, kind: str) -> list[tuple]:
        return [row for row in self.progress if row[0] == kind]

    def started(self, name: str) -> tuple:
        rows = [row for row in self.events("tool.started") if row[1] == name]
        assert rows, f"no tool.started projected for {name}: {self.progress}"
        return rows[0]

    def completed(self, name: str) -> tuple:
        rows = [row for row in self.events("tool.completed") if row[1] == name]
        assert rows, f"no tool.completed projected for {name}: {self.progress}"
        return rows[0]

    @property
    def streamed(self) -> str:
        return "".join(self.deltas)

    def dumped(self) -> str:
        """Every projected payload as one string, for leak assertions."""
        return json.dumps(
            [self.progress, self.starts, self.completes, self.deltas, self.reasoning],
            default=repr,
        )


@pytest.fixture
def tool_turn(tmp_path, monkeypatch):
    """One completed turn in ``tools`` mode, with every surface captured."""
    agent = _agent_with(tmp_path, "tools", monkeypatch)
    surfaces = _Surfaces(agent)
    result = agent.run_conversation("read the notes")
    yield agent, surfaces, result
    agent.close()


class TestToolActivityProjection:
    """Plan check 1 — a tool-using turn shows structured activity."""

    def test_a_tool_call_projects_a_started_event(self, tool_turn):
        _, surfaces, _ = tool_turn
        event, name, preview, args, _ = surfaces.started("Read")
        assert event == "tool.started"
        assert args["file_path"] == "notes.txt"
        assert preview

    def test_a_tool_call_projects_a_completed_event_with_its_result(self, tool_turn):
        _, surfaces, _ = tool_turn
        _, _, _, _, kwargs = surfaces.completed("Read")
        assert kwargs["is_error"] is False
        assert "42" in kwargs["result"]

    def test_started_and_completed_share_one_stable_call_id(self, tool_turn):
        _, surfaces, _ = tool_turn
        start_ids = {call_id for call_id, name, _ in surfaces.starts if name == "Read"}
        done_ids = {
            call_id for call_id, name, _, _ in surfaces.completes if name == "Read"
        }
        assert start_ids and start_ids == done_ids
        # Stable, not random: correlating a live tool card with the same call
        # after a resume is what the deterministic id buys.
        assert all("tc-read" in call_id for call_id in start_ids)

    def test_a_failed_tool_is_projected_as_an_error(self, tool_turn):
        _, surfaces, _ = tool_turn
        _, _, _, _, kwargs = surfaces.completed("Bash")
        assert kwargs["is_error"] is True

    def test_a_completed_tool_reports_a_duration(self, tool_turn):
        _, surfaces, _ = tool_turn
        _, _, _, _, kwargs = surfaces.completed("Read")
        assert isinstance(kwargs["duration"], float)
        assert kwargs["duration"] >= 0

    def test_an_update_only_tool_call_still_gets_a_started_event(self, tool_turn):
        """A tool the client only ever sees as an update must not lose its
        opening event, or a structured tool card never opens for it."""
        _, surfaces, _ = tool_turn
        surfaces.started("Grep")
        surfaces.completed("Grep")

    def test_an_already_completed_tool_call_still_gets_a_started_event(
        self, tool_turn
    ):
        """Same hazard from the other direction: a call fast enough to arrive
        already `completed` must not close a card that never opened."""
        _, surfaces, _ = tool_turn
        surfaces.started("Glob")
        surfaces.completed("Glob")

    def test_a_non_terminal_status_is_not_reported_as_completion(self, tool_turn):
        """`in_progress` is a status change, not an outcome: the tool that gets
        one must still complete exactly once."""
        _, surfaces, _ = tool_turn
        completions = surfaces.events("tool.completed")
        names = [row[1] for row in completions]
        assert len(names) == len(set(names))
        assert set(names) == {"Read", "Bash", "Grep", "Glob"}

    def test_tool_iterations_advance_the_skill_counter(self, tmp_path, monkeypatch):
        """The nudge gate counts iterations of work; a multi-tool turn is not
        one iteration."""
        agent = _agent_with(tmp_path, "tools", monkeypatch)
        surfaces = _Surfaces(agent)
        agent._iters_since_skill = 0
        try:
            agent.run_conversation("read the notes")
        finally:
            agent.close()
        assert agent._iters_since_skill == len(surfaces.events("tool.completed")) > 1


class TestNoRawPayloadLeaks:
    """The direct-CLI failure this runtime replaces: protocol text in the reply."""

    def test_the_response_is_only_assistant_text(self, tool_turn):
        _, _, result = tool_turn
        assert result["final_response"] == "Reading. Done."

    @pytest.mark.parametrize(
        "leak",
        [
            "tc-read", "rawOutput", "rawInput", "sessionUpdate", "toolCallId",
            "ECHOED-USER-PROMPT", "THOUGHT-not-response", "PLAN-ENTRY",
            "COMMAND-DESC", "usage_update",
        ],
    )
    def test_no_protocol_payload_reaches_the_response(self, tool_turn, leak):
        _, _, result = tool_turn
        assert leak not in result["final_response"]

    def test_no_protocol_payload_reaches_the_stream(self, tool_turn):
        _, surfaces, result = tool_turn
        assert surfaces.streamed == result["final_response"]

    def test_the_transcript_row_matches_the_response(self, tool_turn):
        _, _, result = tool_turn
        assert result["messages"][-1] == {
            "role": "assistant", "content": "Reading. Done."
        }


class TestCredentialRedaction:
    """Safety invariant — credential scrubbing on every projected field."""

    def test_a_secret_in_raw_input_never_reaches_a_display_surface(self, tool_turn):
        _, surfaces, _ = tool_turn
        assert fake.TOOL_INPUT_SECRET not in surfaces.dumped()

    def test_a_secret_in_raw_output_never_reaches_a_display_surface(self, tool_turn):
        _, surfaces, _ = tool_turn
        assert fake.TOOL_OUTPUT_SECRET not in surfaces.dumped()

    def test_the_secret_field_is_scrubbed_rather_than_dropped(self, tool_turn):
        """Redaction, not deletion: the arg is still shown, masked."""
        _, surfaces, _ = tool_turn
        _, _, _, args, _ = surfaces.started("Read")
        assert "api_key" in args
        assert fake.TOOL_INPUT_SECRET not in str(args["api_key"])

    def test_non_secret_arguments_survive_scrubbing(self, tool_turn):
        _, surfaces, _ = tool_turn
        _, _, _, args, _ = surfaces.started("Read")
        assert args["file_path"] == "notes.txt"

    def test_a_title_derived_preview_is_scrubbed(self, tool_turn):
        _, surfaces, _ = tool_turn
        _, _, preview, _, _ = surfaces.started("Read")
        assert fake.TOOL_INPUT_SECRET not in preview


class TestStreamingProjection:
    """`agent_message_chunk` reaches the existing streaming surface."""

    def test_assistant_chunks_arrive_as_deltas_in_order(self, tool_turn):
        _, surfaces, _ = tool_turn
        assert surfaces.deltas[0].startswith("Reading.")
        assert surfaces.deltas[-1].endswith("Done.")

    def test_user_message_chunks_never_reach_the_stream(self, tool_turn):
        _, surfaces, _ = tool_turn
        assert "ECHOED-USER-PROMPT" not in surfaces.streamed

    def test_thoughts_reach_the_reasoning_surface_not_the_stream(self, tool_turn):
        _, surfaces, _ = tool_turn
        assert "THOUGHT-not-response" in "".join(surfaces.reasoning)
        assert "THOUGHT-not-response" not in surfaces.streamed

    def test_a_held_back_partial_tag_is_flushed_at_turn_end(
        self, tmp_path, monkeypatch
    ):
        """The stream scrubber holds a trailing '<' pending a possible
        `<think>`. A surface that already streamed the reply does not re-render
        it, so an unflushed tail is text the user never sees."""
        agent = _agent_with(tmp_path, "partial-tag", monkeypatch)
        surfaces = _Surfaces(agent)
        try:
            result = agent.run_conversation("say something")
        finally:
            agent.close()
        assert result["final_response"] == "trailing bracket <"
        assert surfaces.streamed == result["final_response"]

    def test_a_raising_display_callback_does_not_break_the_turn(
        self, tmp_path, monkeypatch
    ):
        agent = _agent_with(tmp_path, "tools", monkeypatch)

        def _boom(*args, **kwargs):
            raise RuntimeError("display layer exploded")

        agent.tool_progress_callback = _boom
        agent.tool_start_callback = _boom
        agent.tool_complete_callback = _boom
        agent.stream_delta_callback = _boom
        agent.reasoning_callback = _boom
        try:
            result = agent.run_conversation("read the notes")
        finally:
            agent.close()
        assert result["completed"] is True
        assert result["final_response"] == "Reading. Done."


class TestUsageProjection:
    """`usage_update` and the reply's `usage` reach Hermes' usage accounting."""

    def test_per_turn_tokens_land_on_the_session_counters(self, tool_turn):
        agent, _, _ = tool_turn
        assert agent.session_input_tokens == fake.TURN_USAGE["inputTokens"]
        assert agent.session_output_tokens == fake.TURN_USAGE["outputTokens"]
        assert agent.session_cache_read_tokens == fake.TURN_USAGE["cachedReadTokens"]
        assert agent.session_cache_write_tokens == fake.TURN_USAGE["cachedWriteTokens"]
        assert agent.session_total_tokens == fake.TURN_USAGE["totalTokens"]

    def test_the_result_dict_carries_the_turn_usage(self, tool_turn):
        _, _, result = tool_turn
        assert result["total_tokens"] == fake.TURN_USAGE["totalTokens"]
        assert result["completion_tokens"] == fake.TURN_USAGE["outputTokens"]
        # Hermes' canonical prompt bucket is uncached input plus both cache
        # buckets, so the ACP fields must land in it exactly once each.
        assert result["prompt_tokens"] == (
            result["input_tokens"]
            + result["cache_read_tokens"]
            + result["cache_write_tokens"]
        )
        assert result["prompt_tokens"] + result["completion_tokens"] == (
            result["total_tokens"]
        )
        assert result["reasoning_tokens"] == fake.TURN_USAGE["thoughtTokens"]

    def test_a_subscription_turn_is_priced_as_included(self, tool_turn):
        """Claude Code bills the user's subscription, not an API account —
        reporting 'unknown' cost for it is misleading accounting."""
        _, _, result = tool_turn
        assert result["cost_status"] == "included"

    def test_context_occupancy_is_projected_separately_from_tokens(self, tool_turn):
        """`usage_update.used` is context occupancy, not this turn's tokens.
        Summing it into the counters would inflate them by ~20x per turn."""
        agent, _, result = tool_turn
        assert result["context_used_tokens"] == fake.CONTEXT_USED_TOKENS
        assert result["context_window_tokens"] == fake.CONTEXT_WINDOW_TOKENS
        assert agent.session_total_tokens == fake.TURN_USAGE["totalTokens"]

    def test_two_turns_accumulate_rather_than_overwrite(self, tmp_path, monkeypatch):
        agent = _agent_with(tmp_path, "tools", monkeypatch)
        try:
            agent.run_conversation("read the notes")
            agent.run_conversation("read them again")
        finally:
            agent.close()
        assert agent.session_output_tokens == 2 * fake.TURN_USAGE["outputTokens"]
        assert agent.session_total_tokens == 2 * fake.TURN_USAGE["totalTokens"]

    def test_a_turn_without_usage_still_counts_one_api_call(
        self, tmp_path, monkeypatch
    ):
        agent = _agent_with(tmp_path, "normal", monkeypatch)
        before = agent.session_api_calls
        try:
            result = agent.run_conversation("what is the answer?")
        finally:
            agent.close()
        assert result["completed"] is True
        assert agent.session_api_calls == before + 1
        assert agent.session_total_tokens == 0

    def test_one_turn_counts_exactly_one_api_call(self, tool_turn):
        agent, _, _ = tool_turn
        assert agent.session_api_calls == 1

    def test_hermes_side_compression_is_not_driven_by_projected_usage(
        self, tmp_path, monkeypatch
    ):
        """Claude Code owns and compacts its own context window, and Hermes
        never replays its transcript into the adapter. Feeding the Hermes
        compressor here would rewrite past context for no benefit."""
        agent = _agent_with(tmp_path, "tools", monkeypatch)
        compressor = agent.context_compressor
        assert compressor is not None, "expected a real compressor to spy on"
        seen: list = []
        monkeypatch.setattr(
            compressor, "update_from_response", lambda usage: seen.append(usage)
        )
        try:
            agent.run_conversation("read the notes")
        finally:
            agent.close()
        assert seen == []

    def test_a_quiet_turn_keeps_the_occupancy_the_session_last_reported(
        self, tmp_path, monkeypatch
    ):
        """`usage_update` arrives on a per-turn projector, so a turn that emits
        none used to report no occupancy at all — and any display of it (the
        gateway runtime footer, Packet 5 slice B) flickered between a real
        percentage and none, turn by turn. How full the window is, is a
        standing fact about the *session*: the adapter's last statement about
        it stands until the adapter makes another."""
        agent = _agent_with(tmp_path, "usage-once", monkeypatch)
        try:
            loud = agent.run_conversation("first")
            quiet = agent.run_conversation("second")
        finally:
            agent.close()
        assert loud["context_used_tokens"] == fake.CONTEXT_USED_TOKENS
        assert quiet["context_used_tokens"] == fake.CONTEXT_USED_TOKENS
        assert quiet["context_window_tokens"] == fake.CONTEXT_WINDOW_TOKENS

    def test_a_carried_over_occupancy_carries_no_cost(self, tmp_path, monkeypatch):
        """Occupancy is a standing fact; a cost is an event. The adapter's
        figure is what one turn would have cost on API billing, so re-reporting
        it for a turn that reported none would invent spend that never
        happened."""
        agent = _agent_with(tmp_path, "usage-once", monkeypatch)
        try:
            loud = agent.run_conversation("first")
            quiet = agent.run_conversation("second")
        finally:
            agent.close()
        assert loud["adapter_reported_cost_usd"] == fake.TURN_COST_USD
        assert "adapter_reported_cost_usd" not in quiet


class TestInvariantsPreserved:
    """Cross-cutting invariants must survive the new projection."""

    def test_role_alternation_holds(self, tool_turn):
        """Since Packet 5 slice D a tool-using turn splices its tool activity
        between the user row and the reply (``user`` →
        ``[assistant(tool_calls), tool]*`` → ``assistant(text)``), the shape
        ``agent/codex_runtime.py`` has always produced. What must hold is the
        invariant, not the old row count: no two adjacent rows share a role, and
        the turn still ends on the assistant's answer."""
        _, _, result = tool_turn
        roles = [m["role"] for m in result["messages"]]
        assert roles[0] == "user"
        assert roles[-1] == "assistant"
        assert all(a != b for a, b in zip(roles, roles[1:]))
        assert roles.count("tool") == 4

    def test_only_the_new_user_turn_is_sent(self, tmp_path, monkeypatch):
        """Prompt caching is sacred: projection must not start replaying
        history into the adapter."""
        record = tmp_path / "prompt.json"
        agent = _agent_with(tmp_path, "tools", monkeypatch, record=record)
        history = [
            {"role": "user", "content": "an earlier question"},
            {"role": "assistant", "content": "an earlier answer"},
        ]
        try:
            agent.run_conversation("read the notes", conversation_history=history)
        finally:
            agent.close()
        sent = json.loads(record.read_text(encoding="utf-8"))["prompt"]
        assert [block["text"] for block in sent["prompt"]] == ["read the notes"]
        assert "an earlier question" not in json.dumps(sent)

    def test_projection_never_touches_a_reported_location(self, tool_turn):
        """Workspace containment: a location the agent reports is display data.
        The projector must not open, create, or resolve it."""
        agent, _, _ = tool_turn
        assert not os.path.exists(
            os.path.join(agent.session_cwd, fake.REPORTED_LOCATION)
        )

    def test_a_permission_request_is_denied_with_no_approval_surface(
        self, tmp_path, monkeypatch
    ):
        """Deny is still the default when nobody can be asked.

        Slice 3B routes permission requests to Hermes' approval flow, but this
        turn runs with no interactive CLI, no gateway, and no registered
        callback — so the gate fails closed and the write never happens.
        The bridge itself is covered by
        ``tests/agent/test_claude_code_permission_bridge.py``."""
        agent = _agent_with(tmp_path, "permission", monkeypatch)
        try:
            result = agent.run_conversation("write a file")
        finally:
            agent.close()
        assert "declined" in result["final_response"].lower()
        assert not os.path.exists(os.path.join(str(tmp_path), "permission-probe.txt"))
