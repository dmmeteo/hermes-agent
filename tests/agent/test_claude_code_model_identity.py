"""Packet 4 slice A — authoritative Claude Code model identity.

Replaces the static pseudo-model (``"claude-code"``,
``plugins/model-providers/claude-code-acp``) on Hermes' runtime/usage/footer
surfaces with the model id ACP actually reports, while preserving the exact
authoritative id for internal accounting and showing a concise normalized
label (e.g. ``sonnet-5``) on display surfaces.

Scope: state capture/projection and display/usage identity only. User-driven
model switching and ``/model`` UX for this provider are out of scope (slice
B) — nothing here lets Hermes pick or change the underlying model.

Every integration test spawns a REAL fake adapter subprocess over REAL stdio,
matching the rest of this runtime's test suite (mocks would not exercise the
``configOptions`` wire shape this depends on).
"""

from __future__ import annotations

import os
import sys

import pytest

import run_agent
from agent.claude_code_runtime import (
    claude_acp_display_model,
    claude_acp_model_identity,
    normalize_claude_model_label,
)
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


def _agent_with(tmp_path, mode, monkeypatch, *, session_db=None, session_id="", **env):
    monkeypatch.setenv("FAKE_ACP_MODE", mode)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    extra = {}
    if session_db is not None:
        extra = {"session_db": session_db, "session_id": session_id or "sess-identity"}
    a = _make_agent(**extra)
    a.acp_command = sys.executable
    a.acp_args = [_FAKE_ADAPTER]
    a.session_cwd = str(tmp_path)
    a._skill_nudge_interval = 0
    return a


class TestNormalizeClaudeModelLabel:
    """Pure function: authoritative id -> concise display label."""

    def test_strips_vendor_prefix_and_date_suffix(self):
        assert normalize_claude_model_label("claude-sonnet-5-20260815") == "sonnet-5"

    def test_handles_multi_segment_version_numbers(self):
        assert normalize_claude_model_label("claude-opus-4-1-20250805") == "opus-4-1"

    def test_passes_through_an_alias_unchanged(self):
        assert normalize_claude_model_label("sonnet") == "sonnet"
        assert normalize_claude_model_label("default") == "default"

    def test_passes_through_a_shape_with_no_date_suffix(self):
        assert normalize_claude_model_label("claude-sonnet") == "sonnet"

    def test_blank_input_yields_blank_output(self):
        assert normalize_claude_model_label("") == ""
        assert normalize_claude_model_label("   ") == ""

    def test_strips_surrounding_whitespace(self):
        assert normalize_claude_model_label("  claude-haiku-4-5-20251001  ") == "haiku-4-5"

    def test_the_vendor_prefix_is_recognized_the_same_way_the_predicate_does(self):
        """One rule for "this is a Claude id", read by both halves.

        ``_is_authoritative_model_id`` folds case before testing the prefix, so
        a differently-cased id classifies as an id; the shortener has to strip
        the same prefix it just accepted, or that id would be *called* an id and
        *displayed* with ``Claude-`` still on the front.
        """
        from agent.claude_code_runtime import _is_authoritative_model_id

        for spelling in ("Claude-Sonnet-5-20260815", "CLAUDE-sonnet-5-20260815"):
            assert _is_authoritative_model_id(spelling) is True
            assert "laude-" not in normalize_claude_model_label(spelling)
        assert normalize_claude_model_label("Claude-Sonnet-5-20260815") == "Sonnet-5"

    def test_shortens_a_context_lane_id(self):
        """``claude-sonnet-5-20260815[1m]`` is a spelling the adapter genuinely
        carries (the 1M-token context lane). The date suffix is still noise
        there, and the lane marker is the part worth keeping."""
        assert (
            normalize_claude_model_label("claude-sonnet-5-20260815[1m]")
            == "sonnet-5[1m]"
        )


class TestIdentityIsNoneBeforeAnyTurn:
    def test_no_identity_before_a_turn_completes(self, tmp_path, monkeypatch):
        agent = _agent_with(tmp_path, "normal", monkeypatch)
        assert claude_acp_model_identity(agent) is None
        assert claude_acp_display_model(agent) is None

    def test_a_non_acp_agent_has_no_identity(self):
        agent = run_agent.AIAgent(
            api_key="k", base_url="https://example.test", provider="custom",
            model="some-model", quiet_mode=True, skip_context_files=True,
            skip_memory=True,
        )
        assert claude_acp_model_identity(agent) is None
        assert claude_acp_display_model(agent) is None


class TestIdentityCapturedAfterATurn:
    def test_display_model_is_the_normalized_label(self, tmp_path, monkeypatch):
        agent = _agent_with(tmp_path, "normal", monkeypatch)
        try:
            result = agent.run_conversation("what is the answer?")
        finally:
            agent.close()
        assert result["completed"] is True
        assert claude_acp_display_model(agent) == "sonnet-5"

    def test_the_exact_authoritative_id_is_preserved(self, tmp_path, monkeypatch):
        agent = _agent_with(tmp_path, "normal", monkeypatch)
        try:
            agent.run_conversation("what is the answer?")
        finally:
            agent.close()
        identity = claude_acp_model_identity(agent)
        assert identity is not None
        assert identity.model_id == "claude-sonnet-5-20260815"
        assert identity.label == "sonnet-5"

    def test_agent_model_itself_is_left_untouched(self, tmp_path, monkeypatch):
        """agent.model stays the static pseudo-model.

        It also drives provider resolution, config persistence and the
        gateway's own-model drift check (gateway/run.py's post-turn
        fallback-eviction: `_agent.model != _cfg_model`) — mutating it would
        make every successful Claude Code turn look like an unintentional
        provider-level fallback and evict the cached agent, destroying
        adapter/session reuse (Packet 2). The authoritative identity is
        carried on a side channel instead; see `claude_acp_display_model`.
        """
        agent = _agent_with(tmp_path, "normal", monkeypatch)
        before = agent.model
        try:
            agent.run_conversation("what is the answer?")
        finally:
            agent.close()
        assert agent.model == before
        assert claude_acp_display_model(agent) != agent.model

    def test_a_custom_model_id_normalizes_accordingly(self, tmp_path, monkeypatch):
        agent = _agent_with(
            tmp_path, "normal", monkeypatch,
            FAKE_ACP_MODEL_ID="claude-opus-4-1-20250805",
        )
        try:
            agent.run_conversation("what is the answer?")
        finally:
            agent.close()
        assert claude_acp_display_model(agent) == "opus-4-1"


class TestAutomaticModelFallbackIsCaptured:
    """The SDK's own refusal-fallback switches the live model mid-turn.

    ACP surfaces this as an unprompted `config_option_update` (see
    `_fake_claude_acp_adapter.py`'s `model-fallback` mode and
    `claude_acp_event_projector.py`'s `_project_config_option_update`) — there
    is no separate "fallback happened" event. Slice A only has to notice the
    identity changed; it does not add any new user-facing fallback UX.
    """

    def test_the_display_label_reflects_the_post_fallback_model(
        self, tmp_path, monkeypatch
    ):
        agent = _agent_with(tmp_path, "model-fallback", monkeypatch)
        try:
            result = agent.run_conversation("hard question")
        finally:
            agent.close()
        assert result["completed"] is True
        assert claude_acp_display_model(agent) == "haiku-4-5"

    def test_the_raw_authoritative_id_reflects_the_fallback_too(
        self, tmp_path, monkeypatch
    ):
        agent = _agent_with(tmp_path, "model-fallback", monkeypatch)
        try:
            agent.run_conversation("hard question")
        finally:
            agent.close()
        identity = claude_acp_model_identity(agent)
        assert identity is not None
        assert identity.model_id == "claude-haiku-4-5-20251001"

    def test_no_new_display_bubble_is_fired_for_the_model_change(
        self, tmp_path, monkeypatch
    ):
        """Non-scope: slice A captures identity, it does not add /model UX or
        a chat-visible "model changed" notice."""
        agent = _agent_with(tmp_path, "model-fallback", monkeypatch)
        progress: list = []
        agent.tool_progress_callback = lambda *a, **kw: progress.append((a, kw))
        try:
            agent.run_conversation("hard question")
        finally:
            agent.close()
        assert progress == []

    def test_a_later_turn_keeps_the_fallback_model_as_current(
        self, tmp_path, monkeypatch
    ):
        """The session persists the switched-to identity across turns."""
        agent = _agent_with(tmp_path, "model-fallback", monkeypatch)
        try:
            agent.run_conversation("hard question")
            history = agent.run_conversation("another one")["messages"]
            assert history  # sanity: the second turn actually ran
        finally:
            agent.close()
        assert claude_acp_display_model(agent) == "haiku-4-5"


class TestASelectorThatIsNotAnAuthoritativeId:
    """``configOptions``' ``currentValue`` is the adapter's picker *selector*.

    It is a model id only some of the time. The pinned adapter always prepends
    a ``"default"`` row to the advertised list
    (``applyAvailableModelsAllowlist``) and picks ``models[0]`` as current when
    neither ``ANTHROPIC_MODEL`` nor ``settings.model`` is set, so a user whose
    ``~/.claude/settings.json`` carries an ``availableModels`` allowlist and no
    model override gets ``currentValue == "default"`` — and family aliases
    (``sonnet``) resolve the same way. The SDK's canonical id for such a row
    (``ModelInfo.resolvedModel``) is dropped by the adapter and never reaches
    the wire, so Hermes cannot recover it: what it can do is never *present*
    or *persist* a selector as though it were the id.
    """

    def _agent(self, tmp_path, monkeypatch, selector, options):
        return _agent_with(
            tmp_path, "normal", monkeypatch,
            FAKE_ACP_MODEL_ID=selector,
            FAKE_ACP_MODEL_OPTIONS=options,
        )

    def test_the_default_selector_displays_the_advertised_name(
        self, tmp_path, monkeypatch
    ):
        agent = self._agent(
            tmp_path, monkeypatch, "default",
            "default,claude-sonnet-5-20260815,claude-haiku-4-5-20251001",
        )
        try:
            agent.run_conversation("what is the answer?")
        finally:
            agent.close()
        assert claude_acp_display_model(agent) == "Default"

    def test_the_label_is_never_a_bare_selector(self, tmp_path, monkeypatch):
        """The contract that keeps this regression from returning: whatever the
        adapter reports, a display surface never shows a raw picker token."""
        agent = self._agent(
            tmp_path, monkeypatch, "default", "default,claude-sonnet-5-20260815",
        )
        try:
            agent.run_conversation("what is the answer?")
        finally:
            agent.close()
        assert claude_acp_display_model(agent) not in ("default", "claude-code")

    def test_an_alias_selector_displays_the_advertised_name(
        self, tmp_path, monkeypatch
    ):
        agent = self._agent(
            tmp_path, monkeypatch, "sonnet", "default,sonnet,claude-haiku-4-5-20251001",
        )
        try:
            agent.run_conversation("what is the answer?")
        finally:
            agent.close()
        assert claude_acp_display_model(agent) == "Sonnet 5"

    def test_the_selector_itself_is_still_carried_verbatim(
        self, tmp_path, monkeypatch
    ):
        """The raw value is what a later pick and any wire comparison need; it
        is the *interpretation* of it as an authoritative id that was wrong."""
        agent = self._agent(
            tmp_path, monkeypatch, "default", "default,claude-sonnet-5-20260815",
        )
        try:
            agent.run_conversation("what is the answer?")
        finally:
            agent.close()
        identity = claude_acp_model_identity(agent)
        assert identity.model_id == "default"
        assert identity.is_authoritative is False

    def test_a_dated_id_is_still_authoritative(self, tmp_path, monkeypatch):
        agent = _agent_with(tmp_path, "normal", monkeypatch)
        try:
            agent.run_conversation("what is the answer?")
        finally:
            agent.close()
        identity = claude_acp_model_identity(agent)
        assert identity.is_authoritative is True
        assert identity.label == "sonnet-5"

    def test_a_selector_the_wire_never_named_is_still_not_shown_bare(
        self, tmp_path, monkeypatch
    ):
        """The one real-wire path to an unnamed row, driven through the adapter.

        ``ModelInfo.displayName`` is required, so a row never arrives with the
        key missing — but ``applyAvailableModelsAllowlist``'s no-SDK-match
        branch sets ``displayName`` to the value itself, which is what an
        ``availableModels`` entry the SDK resolves nothing for produces. The
        row then names no model, and the footer must still not read
        "the model is ``team-default``".
        """
        agent = self._agent(
            tmp_path, monkeypatch, "team-default",
            "team-default,claude-sonnet-5-20260815",
        )
        try:
            agent.run_conversation("what is the answer?")
        finally:
            agent.close()
        assert claude_acp_display_model(agent) == "Team-default"

    def test_an_unnamed_row_is_named_whether_the_name_is_absent_or_echoed(self):
        """Both spellings of "this row has no name" land on the same label, so
        the invariant does not depend on which one the adapter chose."""
        from agent.claude_code_runtime import _option_label

        assert _option_label("default", "") == "Default"
        assert _option_label("default", "default") == "Default"
        assert _option_label("sonnet", "  sonnet  ") == "Sonnet"
        # A real name still wins, and an id is still shortened.
        assert _option_label("default", "Default") == "Default"
        assert _option_label("sonnet", "Sonnet 5") == "Sonnet 5"
        assert _option_label("claude-sonnet-5-20260815", "sonnet-5") == "sonnet-5"

    def test_accounting_does_not_persist_a_selector_as_a_model(
        self, tmp_path, monkeypatch
    ):
        """``session_model_usage`` exists to attribute tokens per model
        (issue #51607). Keying it on ``"default"`` would invent a model name
        that no row can ever be reconciled against, so an unresolvable selector
        falls back to the provider's pseudo-model — the honest "we were not
        told which model ran"."""
        from agent.claude_code_runtime import _usage_model_label

        agent = self._agent(
            tmp_path, monkeypatch, "default", "default,claude-sonnet-5-20260815",
        )
        try:
            agent.run_conversation("what is the answer?")
            # `agent.model` is the provider's own static value — "claude-code"
            # in a configured install, whatever the harness resolved here.
            assert _usage_model_label(agent) == agent.model
            assert _usage_model_label(agent) != "default"
        finally:
            agent.close()

    def test_the_persisted_usage_row_never_carries_a_selector(
        self, tmp_path, monkeypatch
    ):
        """The same rule, asserted where it actually lands. A ``"default"`` row
        in ``session_model_usage`` would merge every model the session ran
        under a name that is not a model — the failure the per-model
        attribution table exists to prevent."""
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "sessions.db")
        agent = _agent_with(
            tmp_path, "tools", monkeypatch,
            session_db=db, session_id="sess-selector-row",
            FAKE_ACP_MODEL_ID="default",
            FAKE_ACP_MODEL_OPTIONS="default,claude-sonnet-5-20260815",
        )
        pseudo_model = agent.model
        try:
            agent.run_conversation("read the notes")
        finally:
            agent.close()
        assert db.flush_token_counts(timeout=10.0)
        with db._lock:
            models = [
                row["model"]
                for row in db._conn.execute(
                    "SELECT model FROM session_model_usage WHERE session_id = ?",
                    ("sess-selector-row",),
                ).fetchall()
            ]
        db.close()
        assert "default" not in models
        assert models == [pseudo_model or "unknown"]

    def test_accounting_uses_the_id_whenever_the_adapter_reports_one(
        self, tmp_path, monkeypatch
    ):
        from agent.claude_code_runtime import _usage_model_label

        agent = _agent_with(tmp_path, "normal", monkeypatch)
        try:
            agent.run_conversation("what is the answer?")
            assert _usage_model_label(agent) == "claude-sonnet-5-20260815"
        finally:
            agent.close()


class TestUsageAccountingUsesTheAuthoritativeId:
    """SessionDB usage rows persist the exact id, not the pseudo-model."""

    def test_the_persisted_usage_row_is_keyed_on_the_authoritative_id(
        self, tmp_path, monkeypatch
    ):
        """The half of acceptance check #4 that lives in SQLite.

        ``session_model_usage`` is what makes per-model attribution possible
        across a mid-session switch (issue #51607), so the row this runtime
        writes has to carry the model ACP reported — not the pseudo-model, and
        not the concise display label.
        """
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "sessions.db")
        agent = _agent_with(
            tmp_path, "tools", monkeypatch,
            session_db=db, session_id="sess-usage-row",
        )
        try:
            agent.run_conversation("read the notes")
        finally:
            agent.close()
        assert db.flush_token_counts(timeout=10.0)
        with db._lock:
            rows = [
                dict(row)
                for row in db._conn.execute(
                    "SELECT model, api_call_count, billing_mode FROM"
                    " session_model_usage WHERE session_id = ?",
                    ("sess-usage-row",),
                ).fetchall()
            ]
        db.close()
        assert [row["model"] for row in rows] == ["claude-sonnet-5-20260815"]
        assert rows[0]["api_call_count"] >= 1
        assert rows[0]["billing_mode"] == "subscription_included"

    def test_queued_token_counts_use_the_raw_authoritative_id(
        self, tmp_path, monkeypatch
    ):
        agent = _agent_with(tmp_path, "tools", monkeypatch)
        agent._session_db = None  # no real DB — assert on the estimator input instead

        seen_models: list[str] = []
        import agent.usage_pricing as usage_pricing

        real_estimate = usage_pricing.estimate_usage_cost

        def _spy(model_name, usage, **kw):
            seen_models.append(model_name)
            return real_estimate(model_name, usage, **kw)

        # _record_usage imports estimate_usage_cost locally from
        # agent.usage_pricing on every call, so the module-level name (not a
        # claude_code_runtime-local one) is what has to be patched.
        monkeypatch.setattr("agent.usage_pricing.estimate_usage_cost", _spy)
        try:
            agent.run_conversation("read the notes")
        finally:
            agent.close()
        assert seen_models == ["claude-sonnet-5-20260815"]

    def test_usage_is_still_reported_as_subscription_included(
        self, tmp_path, monkeypatch
    ):
        """Changing which model string prices the turn must not change the
        subscription billing route — that is keyed on provider, not model
        (agent/usage_pricing.py's resolve_billing_route)."""
        agent = _agent_with(tmp_path, "tools", monkeypatch)
        try:
            result = agent.run_conversation("read the notes")
        finally:
            agent.close()
        assert result["cost_status"] == "included"

    def test_falls_back_to_the_pseudo_model_before_any_identity_is_known(self):
        """A turn that fails before connecting (e.g. missing adapter) has no
        captured identity yet; usage bookkeeping must not crash on that."""
        from agent.claude_code_runtime import _usage_model_label

        class _Stub:
            model = "claude-code"

        assert _usage_model_label(_Stub()) == "claude-code"
