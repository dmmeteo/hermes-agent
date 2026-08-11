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
"""

from __future__ import annotations

from agent.transports.claude_acp_event_projector import (
    ClaudeAcpEventProjector,
    ProjectedEvent,
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
