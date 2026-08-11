"""Unit tests for ``claude_acp_event_projector``'s model-identity capture.

Packet 4 slice A: ACP has no dedicated "current model" field. The pinned
adapter (0.66.0) reports the session's authoritative model id as the
``currentValue`` of a "model" entry inside the generic ``configOptions`` list
carried on ``session/new``, ``session/load`` and every ``config_option_update``
— including the update the adapter sends unprompted after its own SDK-side
refusal fallback (``syncModelAfterRefusalFallback`` in ``dist/acp-agent.js``
re-uses the exact same path a user-driven switch takes). These are pure-data
tests; the wire capture and end-to-end runtime behaviour are covered in
``tests/agent/transports/test_claude_acp.py`` and
``tests/agent/test_claude_code_model_identity.py`` respectively.

Slice B adds the *selectable* half of the same option: ``extract_model_options``
reads the "model" entry's ``options`` list — the models this live session
actually advertises, which is the only truthful source for a Hermes-side pick.

Packet 5 slice D adds ``projected_messages``: the same terminal tool events the
display surfaces see, as canonical ``(assistant tool_calls, tool)`` transcript
pairs. ``TestTranscriptProjection`` covers the shape, the bounds and the
orphan-repair rule as pure data; the end-to-end persistence is
``tests/agent/test_claude_code_transcript_projection.py``.
"""

from __future__ import annotations

import json

from agent.transports.claude_acp_event_projector import (
    PERSISTED_RESULT_MAX_CHARS,
    UNREPORTED_TOOL_RESULT,
    ClaudeAcpEventProjector,
    ProjectedEvent,
    deterministic_call_id,
    extract_available_mode_ids,
    extract_current_mode_id,
    extract_current_model_id,
    extract_model_options,
    has_model_config_option,
)


class TestExtractCurrentModelId:
    def test_reads_the_model_options_current_value(self):
        config_options = [
            {"id": "mode", "type": "select", "currentValue": "default"},
            {
                "id": "model",
                "type": "select",
                "currentValue": "claude-sonnet-5-20260815",
            },
        ]
        assert extract_current_model_id(config_options) == "claude-sonnet-5-20260815"

    def test_none_when_no_model_entry_is_present(self):
        config_options = [{"id": "mode", "type": "select", "currentValue": "default"}]
        assert extract_current_model_id(config_options) is None

    def test_none_for_a_non_list_payload(self):
        assert extract_current_model_id(None) is None
        assert extract_current_model_id({"id": "model"}) is None

    def test_none_when_current_value_is_blank_or_wrong_type(self):
        assert extract_current_model_id([{"id": "model", "currentValue": ""}]) is None
        assert extract_current_model_id([{"id": "model", "currentValue": None}]) is None
        assert extract_current_model_id([{"id": "model", "currentValue": 7}]) is None

    def test_strips_surrounding_whitespace(self):
        config_options = [{"id": "model", "currentValue": "  claude-opus-5  "}]
        assert extract_current_model_id(config_options) == "claude-opus-5"

    def test_ignores_a_malformed_entry(self):
        config_options = ["not-a-dict", {"id": "model", "currentValue": "sonnet-5"}]
        assert extract_current_model_id(config_options) == "sonnet-5"


class TestExtractModelOptions:
    """Packet 4 slice B: the models this session advertises as selectable.

    Same "model" entry ``extract_current_model_id`` reads, different field:
    ``options``. The adapter builds it from ``models.availableModels``
    (``buildConfigOptions`` in ``dist/acp-agent.js``), which is already
    filtered by the user's own ``availableModels`` allowlist — so honouring it
    verbatim is what keeps a Hermes-side pick inside the adapter's own policy
    instead of second-guessing it.
    """

    def test_reads_value_name_and_description_in_order(self):
        options = extract_model_options([
            {"id": "mode", "type": "select", "currentValue": "default",
             "options": [{"value": "default", "name": "Default"}]},
            {
                "id": "model",
                "type": "select",
                "currentValue": "claude-sonnet-5-20260815",
                "options": [
                    {"value": "claude-sonnet-5-20260815", "name": "Sonnet 5",
                     "description": "Balanced"},
                    {"value": "claude-haiku-4-5-20251001", "name": "Haiku 4.5"},
                ],
            },
        ])
        assert options == [
            {"value": "claude-sonnet-5-20260815", "name": "Sonnet 5",
             "description": "Balanced"},
            {"value": "claude-haiku-4-5-20251001", "name": "Haiku 4.5",
             "description": ""},
        ]

    def test_flattens_option_groups(self):
        """ACP's ``SessionConfigSelectOptions`` may be a list of *groups*.

        The adapter flattens the same way when it validates a requested value
        (``"options" in o ? o.options : [o]``), so a grouped list must not read
        as "no models advertised".
        """
        options = extract_model_options([{
            "id": "model",
            "currentValue": "claude-opus-5",
            "options": [
                {"name": "Latest", "options": [
                    {"value": "claude-opus-5", "name": "Opus 5"},
                ]},
                {"value": "claude-haiku-4-5-20251001", "name": "Haiku 4.5"},
            ],
        }])
        assert [entry["value"] for entry in options] == [
            "claude-opus-5", "claude-haiku-4-5-20251001",
        ]

    def test_empty_when_no_model_entry_or_no_options(self):
        assert extract_model_options(None) == []
        assert extract_model_options([{"id": "mode", "options": [{"value": "x"}]}]) == []
        assert extract_model_options([{"id": "model", "currentValue": "x"}]) == []
        assert extract_model_options([{"id": "model", "options": "nope"}]) == []

    def test_skips_malformed_entries_and_blank_values(self):
        options = extract_model_options([{
            "id": "model",
            "options": [
                "not-a-dict",
                {"name": "no value"},
                {"value": "   ", "name": "blank"},
                {"value": 7, "name": "wrong type"},
                {"value": "  claude-opus-5  ", "name": "  Opus 5  "},
            ],
        }])
        assert options == [
            {"value": "claude-opus-5", "name": "Opus 5", "description": ""},
        ]

    def test_a_row_with_no_name_reports_no_name(self):
        """Substituting the value here would erase the one fact the consumer
        needs: that this row carries no human-readable name at all. Display
        code has a rule for that (name the token, don't show it bare —
        ``_option_label``) and cannot apply it to a name it can't tell apart
        from an id."""
        options = extract_model_options(
            [{"id": "model", "options": [{"value": "claude-opus-5"}, {"value": "x"}]}]
        )
        assert options == [
            {"value": "claude-opus-5", "name": "", "description": ""},
            {"value": "x", "name": "", "description": ""},
        ]

    def test_a_name_equal_to_the_value_is_read_verbatim(self):
        """The real-wire shape of "no name": ``ModelInfo.displayName`` is
        required, and the adapter's no-SDK-match branch sets it to the value
        itself (``applyAvailableModelsAllowlist``). This function reads; naming
        that row is the consumer's decision, not this one's."""
        options = extract_model_options(
            [{"id": "model", "options": [{"value": "team-default",
                                         "name": "team-default"}]}]
        )
        assert options == [
            {"value": "team-default", "name": "team-default", "description": ""},
        ]

    def test_deduplicates_repeated_values_keeping_the_first(self):
        options = extract_model_options([{
            "id": "model",
            "options": [
                {"value": "claude-opus-5", "name": "Opus 5"},
                {"value": "claude-opus-5", "name": "Opus 5 (again)"},
            ],
        }])
        assert options == [
            {"value": "claude-opus-5", "name": "Opus 5", "description": ""},
        ]


class TestConfigOptionUpdateProjection:
    def _update(self, current_value):
        return {
            "sessionUpdate": "config_option_update",
            "configOptions": [
                {"id": "mode", "type": "select", "currentValue": "default"},
                {"id": "model", "type": "select", "currentValue": current_value},
            ],
        }

    def test_a_model_change_projects_as_model_changed(self):
        projector = ClaudeAcpEventProjector()
        event = projector.project(self._update("claude-sonnet-5-20260815"))
        assert event.kind == "model_changed"
        assert event.model_id == "claude-sonnet-5-20260815"

    def test_the_projector_remembers_the_latest_model_id(self):
        projector = ClaudeAcpEventProjector()
        projector.project(self._update("claude-sonnet-5-20260815"))
        assert projector.model_id == "claude-sonnet-5-20260815"

    def test_repeating_the_same_model_id_is_ignored(self):
        """An unrelated config change (mode, effort) restates the WHOLE list,
        including a model entry whose value has not actually changed. That
        must not be reported as a fresh identity event every time."""
        projector = ClaudeAcpEventProjector()
        first = projector.project(self._update("claude-sonnet-5-20260815"))
        second = projector.project(self._update("claude-sonnet-5-20260815"))
        assert first.kind == "model_changed"
        assert second.kind == "ignored"

    def test_a_second_distinct_model_id_projects_again(self):
        """The automatic-fallback case: the SDK swaps the live model out from
        under the session, and the adapter reports it via the same event."""
        projector = ClaudeAcpEventProjector()
        projector.project(self._update("claude-sonnet-5-20260815"))
        event = projector.project(self._update("claude-haiku-4-5-20251001"))
        assert event.kind == "model_changed"
        assert event.model_id == "claude-haiku-4-5-20251001"

    def test_no_model_entry_is_ignored(self):
        projector = ClaudeAcpEventProjector()
        event = projector.project({
            "sessionUpdate": "config_option_update",
            "configOptions": [{"id": "mode", "type": "select", "currentValue": "plan"}],
        })
        assert event.kind == "ignored"
        assert projector.model_id is None

    def test_ignored_event_carries_no_model_id(self):
        assert ProjectedEvent(kind="ignored").model_id == ""

    def test_the_first_update_of_a_turn_projects_even_when_nothing_changed(self):
        """The dedupe is per-turn, and a fresh projector knows nothing about
        the session's current model, so the first ``config_option_update`` of a
        turn projects even when it merely restates it. Harmless — the session
        writes the same value back and no display surface is involved — but it
        is what the code does, so it is what the docs must say."""
        projector = ClaudeAcpEventProjector()
        event = projector.project(self._update("claude-sonnet-5-20260815"))
        assert event.kind == "model_changed"
        assert ClaudeAcpEventProjector().model_id is None


class TestHasModelConfigOption:
    """"No model state in this response" and "the model state is empty" are
    different facts, and a reconnect has to tell them apart: the first means
    the response says nothing, the second means the live session advertises
    nothing. Truthiness of the extracted list collapses both."""

    def test_true_when_the_entry_exists_with_options(self):
        assert has_model_config_option([
            {"id": "model", "currentValue": "claude-sonnet-5-20260815",
             "options": [{"value": "claude-sonnet-5-20260815"}]},
        ]) is True

    def test_true_when_the_entry_exists_and_advertises_nothing(self):
        assert has_model_config_option([{"id": "model", "options": []}]) is True
        assert has_model_config_option([{"id": "model"}]) is True

    def test_false_when_no_model_entry_is_present(self):
        assert has_model_config_option([{"id": "mode", "currentValue": "default"}]) is False

    def test_false_for_a_response_with_no_config_options_at_all(self):
        assert has_model_config_option(None) is False
        assert has_model_config_option([]) is False


class TestTranscriptProjection:
    """Packet 5 slice D: terminal tool events as canonical transcript rows.

    The shape is ``agent/transports/codex_event_projector.py``'s, verbatim —
    ``{role: assistant, content: None, tool_calls: [...]}`` followed by
    ``{role: tool, tool_call_id, content}`` — because that is what
    ``agent/curator.py``, ``session_search`` and a later native-model turn
    already know how to read.
    """

    def _call(self, **overrides):
        payload = {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-1",
            "title": "Read notes.txt",
            "kind": "read",
            "status": "pending",
            "rawInput": {"file_path": "notes.txt"},
            "_meta": {"claudeCode": {"toolName": "Read"}},
        }
        payload.update(overrides)
        return payload

    def _done(self, **overrides):
        payload = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-1",
            "status": "completed",
            "rawOutput": "42",
        }
        payload.update(overrides)
        return payload

    def test_a_fresh_projector_has_projected_nothing(self):
        assert ClaudeAcpEventProjector().projected_messages == []

    def test_a_completed_tool_projects_one_assistant_tool_row_and_one_result(self):
        projector = ClaudeAcpEventProjector()
        projector.project(self._call())
        projector.project(self._done())
        assert projector.projected_messages == [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": deterministic_call_id("tc-1"),
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "arguments": json.dumps(
                            {"file_path": "notes.txt"}, ensure_ascii=False,
                            sort_keys=True,
                        ),
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": deterministic_call_id("tc-1"),
                "name": "Read",
                "tool_name": "Read",
                "content": "42",
            },
        ]

    def test_progress_updates_add_no_rows_of_their_own(self):
        """Rows are written per *outcome*, not per progress event — otherwise a
        long-running tool writes a row every time its status changes."""
        projector = ClaudeAcpEventProjector()
        projector.project(self._call())
        for _ in range(3):
            projector.project(self._done(status="in_progress", rawOutput=None))
        assert len(projector.projected_messages) == 2
        projector.project(self._done())
        assert len(projector.projected_messages) == 2

    def test_a_tool_that_arrives_already_completed_projects_one_pair(self):
        projector = ClaudeAcpEventProjector()
        projector.project(self._call(status="completed", rawOutput="2 entries"))
        rows = projector.projected_messages
        assert [row["role"] for row in rows] == ["assistant", "tool"]
        assert rows[1]["content"] == "2 entries"

    def test_an_update_only_tool_projects_one_pair(self):
        projector = ClaudeAcpEventProjector()
        projector.project(self._done(
            toolCallId="tc-orphan", title="Grep", kind="search",
            rawInput={"pattern": "TODO"}, rawOutput="3 matches",
            _meta={"claudeCode": {"toolName": "Grep"}},
        ))
        rows = projector.projected_messages
        assert [row["role"] for row in rows] == ["assistant", "tool"]
        assert rows[1]["tool_name"] == "Grep"

    def test_two_tools_project_two_pairs_in_completion_order(self):
        projector = ClaudeAcpEventProjector()
        projector.project(self._call())
        projector.project(self._call(
            toolCallId="tc-2", _meta={"claudeCode": {"toolName": "Bash"}},
            rawInput={"command": "ls"},
        ))
        projector.project(self._done(toolCallId="tc-2", rawOutput="ok"))
        projector.project(self._done())
        assert [
            row["tool_name"] for row in projector.projected_messages
            if row["role"] == "tool"
        ] == ["Bash", "Read"]

    def test_a_failed_tool_is_marked_in_the_row(self):
        projector = ClaudeAcpEventProjector()
        projector.project(self._call())
        projector.project(self._done(status="failed", rawOutput="boom"))
        rows = projector.projected_messages
        assert rows[1]["content"] == "[failed] boom"

    def test_a_failed_tool_with_no_output_still_reports_the_failure(self):
        projector = ClaudeAcpEventProjector()
        projector.project(self._call())
        projector.project(self._done(status="failed", rawOutput=None))
        assert projector.projected_messages[1]["content"] == "[failed]"

    def test_an_unfinished_tool_is_repaired_rather_than_left_orphaned(self):
        """An assistant row carrying ``tool_calls`` with no matching ``tool``
        row breaks ``repair_message_sequence`` and would break a later
        native-model turn on the same session. A turn that ends mid-tool
        (cancel, error, a stop reason that is not ``end_turn``) is exactly how
        one would be produced."""
        projector = ClaudeAcpEventProjector()
        projector.project(self._call())
        rows = projector.projected_messages
        assert [row["role"] for row in rows] == ["assistant", "tool"]
        assert rows[1]["content"] == UNREPORTED_TOOL_RESULT
        assert rows[1]["tool_call_id"] == rows[0]["tool_calls"][0]["id"]

    def test_repair_covers_a_tool_left_in_progress(self):
        projector = ClaudeAcpEventProjector()
        projector.project(self._call())
        projector.project(self._done(status="in_progress", rawOutput=None))
        rows = projector.projected_messages
        assert rows[1]["content"] == UNREPORTED_TOOL_RESULT

    def test_a_finished_tool_is_never_repaired_on_top_of_its_own_result(self):
        projector = ClaudeAcpEventProjector()
        projector.project(self._call())
        projector.project(self._done())
        assert len(projector.projected_messages) == 2

    def test_a_second_terminal_update_does_not_duplicate_the_pair(self):
        """Two rows under one ``tool_call_id`` would break the pairing that
        ``repair_message_sequence`` — and a later native-model turn — relies on.
        First outcome wins; the display's own handling of a second completion is
        unchanged."""
        projector = ClaudeAcpEventProjector()
        projector.project(self._call())
        projector.project(self._done(rawOutput="first"))
        event = projector.project(self._done(status="failed", rawOutput="second"))
        rows = projector.projected_messages
        assert len(rows) == 2
        assert rows[1]["content"] == "first"
        assert event.kind == "tool_completed"

    def test_reading_the_messages_twice_returns_the_same_rows(self):
        """Read at turn end, and possibly read again by a caller inspecting the
        outcome. Repair must not accumulate."""
        projector = ClaudeAcpEventProjector()
        projector.project(self._call())
        projector.project(self._call(
            toolCallId="tc-2", _meta={"claudeCode": {"toolName": "Bash"}},
        ))
        projector.project(self._done(toolCallId="tc-2", rawOutput="ok"))
        assert projector.projected_messages == projector.projected_messages
        assert len(projector.projected_messages) == 4

    def test_the_returned_list_cannot_be_mutated_into_the_projector(self):
        projector = ClaudeAcpEventProjector()
        projector.project(self._call())
        projector.project(self._done())
        projector.projected_messages.append({"role": "user", "content": "nope"})
        assert len(projector.projected_messages) == 2

    def test_arguments_are_scrubbed_before_they_reach_a_row(self):
        secret = "sk-ant-api03-NOTAREALSECRET0123456789"
        projector = ClaudeAcpEventProjector()
        projector.project(self._call(rawInput={"file_path": "a.py", "key": secret}))
        projector.project(self._done())
        arguments = projector.projected_messages[0]["tool_calls"][0]["function"][
            "arguments"
        ]
        assert secret not in arguments
        assert json.loads(arguments)["file_path"] == "a.py"

    def test_a_result_is_scrubbed_before_it_reaches_a_row(self):
        secret = "ghp_NOTAREALOUTPUTSECRET0123456789"
        projector = ClaudeAcpEventProjector()
        projector.project(self._call())
        projector.project(self._done(rawOutput=f"token {secret}"))
        assert secret not in projector.projected_messages[1]["content"]

    def test_an_oversized_result_is_truncated_with_a_counted_marker(self):
        projector = ClaudeAcpEventProjector()
        projector.project(self._call())
        projector.project(
            self._done(rawOutput="z" * (PERSISTED_RESULT_MAX_CHARS + 1234))
        )
        content = projector.projected_messages[1]["content"]
        assert content.startswith("z" * 100)
        assert content.endswith("… [+1234 chars truncated]")
        assert content.count("z") == PERSISTED_RESULT_MAX_CHARS

    def test_a_result_at_the_bound_is_not_marked(self):
        projector = ClaudeAcpEventProjector()
        projector.project(self._call())
        projector.project(self._done(rawOutput="z" * PERSISTED_RESULT_MAX_CHARS))
        assert projector.projected_messages[1]["content"] == (
            "z" * PERSISTED_RESULT_MAX_CHARS
        )

    def test_the_persisted_result_is_bounded_independently_of_the_display_one(
        self,
    ):
        """The display bubble's bound cuts with a bare ellipsis, which carries
        no count. Deriving the row from it would lose the one thing a
        transcript reader needs to know."""
        projector = ClaudeAcpEventProjector()
        projector.project(self._call())
        event = projector.project(
            self._done(rawOutput="z" * (PERSISTED_RESULT_MAX_CHARS + 10))
        )
        assert event.tool.result.endswith("…")
        assert "chars truncated" not in event.tool.result
        assert projector.projected_messages[1]["content"].endswith(
            "chars truncated]"
        )

    def test_a_diff_or_terminal_content_block_reaches_the_row(self):
        """``rawOutput`` is optional; the ``content`` blocks are what an edit
        reports, and a transcript with no record of an edit is the gap this
        closes."""
        projector = ClaudeAcpEventProjector()
        projector.project(self._call(
            _meta={"claudeCode": {"toolName": "Edit"}}, kind="edit",
        ))
        projector.project(self._done(rawOutput=None, content=[
            {"type": "diff", "path": "a.py"},
        ]))
        assert projector.projected_messages[1]["content"] == "[diff] a.py"

    def test_text_and_usage_events_project_no_rows(self):
        projector = ClaudeAcpEventProjector()
        projector.project({"sessionUpdate": "agent_message_chunk",
                           "content": {"type": "text", "text": "hello"}})
        projector.project({"sessionUpdate": "agent_thought_chunk",
                           "content": {"type": "text", "text": "thinking"}})
        projector.project({"sessionUpdate": "usage_update", "used": 5, "size": 10})
        projector.project({"sessionUpdate": "user_message_chunk",
                           "content": {"type": "text", "text": "echo"}})
        assert projector.projected_messages == []

    def test_the_row_count_tracks_the_tool_iteration_count(self):
        """One pair per completed tool call, which is the same event the skill
        nudge counts — so the two can never disagree about how many tools ran."""
        projector = ClaudeAcpEventProjector()
        for index in range(3):
            projector.project(self._call(toolCallId=f"tc-{index}"))
            projector.project(self._done(toolCallId=f"tc-{index}"))
        assert projector.tool_iterations == 3
        assert len(projector.projected_messages) == 6


class TestExtractModeState:
    """Packet 5 slice A: the ``modes`` block on a session response.

    A first-class ACP field, unlike the model — so this reads
    ``{currentModeId, availableModes}`` and nothing else. It reports; the
    policy in ``agent/claude_acp_permissions.py`` decides.
    """

    def test_reads_the_current_mode(self):
        assert extract_current_mode_id(
            {"currentModeId": "acceptEdits", "availableModes": []}
        ) == "acceptEdits"

    def test_a_blank_or_absent_current_mode_is_none(self):
        assert extract_current_mode_id({"availableModes": []}) is None
        assert extract_current_mode_id({"currentModeId": "  "}) is None
        assert extract_current_mode_id({"currentModeId": 7}) is None
        assert extract_current_mode_id(None) is None

    def test_reads_the_advertised_ids_in_order(self):
        assert extract_available_mode_ids({"availableModes": [
            {"id": "auto", "name": "Auto"},
            {"id": "default", "name": "Default"},
        ]}) == ["auto", "default"]

    def test_a_repeated_id_is_reported_once(self):
        assert extract_available_mode_ids({"availableModes": [
            {"id": "auto"}, {"id": "auto"},
        ]}) == ["auto"]

    def test_malformed_entries_are_dropped_not_guessed(self):
        assert extract_available_mode_ids({"availableModes": [
            {"id": "auto"}, {"name": "no id"}, {"id": ""}, {"id": 3}, None,
        ]}) == ["auto"]

    def test_a_block_that_advertises_nothing_reads_as_empty(self):
        assert extract_available_mode_ids({"currentModeId": "auto"}) == []
        assert extract_available_mode_ids({"availableModes": []}) == []
        assert extract_available_mode_ids(None) == []
