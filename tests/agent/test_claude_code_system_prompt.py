"""Packet 6 — a Hermes operating context inside a Claude Code ACP session.

Before this packet a Claude Code ACP turn ran with **zero** Hermes-authored
context: ``agent/turn_context.py`` builds (and persists) a full system prompt
for every turn including this runtime's, and ``run_claude_code_acp_turn`` then
sends only ``[{"type": "text", "text": user_message}]``.  The model answering
the user did not know it was Hermes, who the user is, or which surface the
conversation is happening on, and had never seen the profile's ``SOUL.md``.

This packet sends a curated Hermes context **once per ACP session** through the
adapter's own extension point — ``_meta.systemPrompt`` on ``session/new`` and
``session/load`` — in the **object form**
(``{"type": "preset", "preset": "claude_code", "append": ...}``) so Claude
Code's own preset, tools and safety rules stay in force.  The string form would
*replace* the preset and is asserted against explicitly: a regression there
silently deletes Claude Code's tool instructions.

What each class pins:

``TestExtractionContracts``   the four behaviour-preserving extractions in
                              ``agent/system_prompt.py`` still produce exactly
                              what the canonical prompt contains (contracts,
                              not snapshots)
``TestConfigResolution``      the pure policy — the default is ``hermes``, and
                              every malformed value resolves to something
                              usable rather than raising
``TestComposition``           what each mode actually contains, and just as
                              importantly what it must NOT contain: the
                              Hermes-only tool guidance, the skills index and
                              the context files that instruct tools this
                              session does not have
``TestBoundingAndDeterminism`` composing twice is byte-identical, and an
                              oversized composition drops whole sections in a
                              fixed order and never drops identity
``TestWireShape``             the object form reaches ``session/new`` /
                              ``session/load`` params, never the string form
``TestSystemPromptReachesTheSession`` REAL fake-adapter subprocesses over REAL
                              stdio: the append lands on the wire, the agent
                              side receives it, it is never replayed per turn,
                              a rebind re-sends the identical bytes, and a
                              composition fault costs a WARNING and not a turn

Confidentiality: every fixture here uses a synthetic marker
(``HERMES-SOUL-MARKER-...``).  No test reads the developer's real
``~/.hermes/SOUL.md`` — ``tests/conftest.py`` already points ``HERMES_HOME`` at
a per-test tempdir, and unlike the skills bridge **nothing here touches
``HOME``**: the composer reads ``get_hermes_home()`` only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import sys
import time

import pytest
import yaml

import run_agent
from agent.claude_acp_system_prompt import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MODE,
    DROP_ORDER,
    MODE_FULL,
    MODE_HERMES,
    MODE_OFF,
    MODE_SOUL,
    MODE_SECTIONS,
    PREAMBLE,
    SECTIONS,
    SystemPromptConfig,
    compose_for_agent,
    compose_system_prompt,
    configured_system_prompt,
    prompt_digest,
    resolve_system_prompt_config,
    system_prompt_meta,
    vocabulary_note,
)
from agent.system_prompt import (
    build_active_profile_hint,
    build_identity_block,
    build_system_prompt,
    prompt_context_length,
    resolve_platform_hint_block,
)
from hermes_state import SessionDB

_FAKE_ADAPTER = os.path.join(os.path.dirname(__file__), "_fake_claude_acp_adapter.py")

_MARKER = "HERMES-SOUL-MARKER-3f9c1a"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="The fake adapter's process-group reaping is POSIX-only; the "
           "composition policy itself is platform-independent.",
)


# --------------------------------------------------------------------- helpers


@pytest.fixture
def soul_md():
    """Write a synthetic SOUL.md into the per-test ``HERMES_HOME``."""

    def _write(text=None, marker=_MARKER):
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
        home.mkdir(parents=True, exist_ok=True)
        body = text if text is not None else (
            f"You are Hermes. {marker}\n\nSpeak plainly and finish the job."
        )
        path = home / "SOUL.md"
        path.write_text(body, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def write_prompt_config():
    """Write ``model.claude_code_acp.system_prompt`` into config.yaml."""

    def _write(value):
        from hermes_cli.config import get_config_path

        config_path = get_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            yaml.safe_dump({"model": {"claude_code_acp": {"system_prompt": value}}}),
            encoding="utf-8",
        )
        return config_path

    return _write


class _StubMemoryStore:
    def __init__(self, memory="", user=""):
        self._blocks = {"memory": memory, "user": user}

    def format_for_system_prompt(self, target):
        return self._blocks.get(target) or None


class _StubMemoryManager:
    def __init__(self, block=""):
        self._block = block

    def build_system_prompt(self):
        return self._block or None


class _StubAgent:
    """The narrow surface ``compose_system_prompt`` is allowed to read.

    Also carries the handful of extra attributes the *canonical* builder reads,
    so the extraction contracts (``TestExtractionContracts``) can assert each
    extracted helper's output against a real ``build_system_prompt`` run rather
    than against a frozen snapshot of it.
    """

    def __init__(self, **kw):
        self.platform = kw.pop("platform", "cli")
        self.model = kw.pop("model", "claude-code")
        self.provider = kw.pop("provider", "claude-code-acp")
        self.valid_tool_names = kw.pop("valid_tool_names", [])
        self.load_soul_identity = kw.pop("load_soul_identity", True)
        self.skip_context_files = kw.pop("skip_context_files", False)
        self.context_compressor = kw.pop("context_compressor", None)
        self._memory_enabled = kw.pop("memory_enabled", True)
        self._user_profile_enabled = kw.pop("user_profile_enabled", True)
        self._memory_store = kw.pop("memory_store", None)
        self._memory_manager = kw.pop("memory_manager", None)
        self._active_system_message = kw.pop("active_system_message", None)
        self._cached_system_prompt = kw.pop("cached_system_prompt", None)
        self._platform_hint_overrides = kw.pop("platform_hint_overrides", None)
        # Read by the canonical builder only.
        self._task_completion_guidance = kw.pop("task_completion_guidance", False)
        self._parallel_tool_call_guidance = kw.pop("parallel_tool_call_guidance", False)
        self._tool_use_enforcement = kw.pop("tool_use_enforcement", False)
        self._environment_probe = kw.pop("environment_probe", False)
        self._kanban_worker_guidance = kw.pop("kanban_worker_guidance", "")
        self.pass_session_id = kw.pop("pass_session_id", False)
        self.session_id = kw.pop("session_id", "")
        self._cached_system_prompt_static = None
        self._use_prompt_caching = False
        for key, value in kw.items():
            setattr(self, key, value)

    def _emit_status(self, message):  # the builder's status channel
        pass


def _agent_with(tmp_path, mode, monkeypatch, *, record=None, record_dir=None,
                store=None, db=None, session_id="", skip_context_files=True,
                **env):
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
        extra = {"session_db": db, "session_id": session_id or "sess-sysprompt"}
    agent = run_agent.AIAgent(
        api_key="claude-code-acp",
        base_url="acp://claude-code",
        provider="claude-code-acp",
        api_mode="claude_code_acp",
        quiet_mode=True,
        skip_context_files=skip_context_files,
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


def _kill(pid: int) -> None:
    import psutil

    os.kill(pid, signal.SIGKILL)
    deadline = time.time() + 10
    while time.time() < deadline and psutil.pid_exists(pid):
        time.sleep(0.02)


def _append_of(params):
    meta = params.get("_meta") or {}
    return (meta.get("systemPrompt") or {}).get("append")


# ---------------------------------------------------- the extraction contracts


class TestExtractionContracts:
    """S2–S5 are pure moves: what they return is what the prompt contains.

    Contracts, not snapshots (AGENTS.md §Don't write change-detector tests):
    each helper's output must be a substring of the canonical prompt built by
    the untouched builder, which is the byte-stability evidence.
    """

    def test_identity_block_is_in_the_canonical_prompt(self, soul_md):
        soul_md()
        agent = _StubAgent()
        text, loaded = build_identity_block(agent, None)
        assert loaded is True
        assert _MARKER in text
        assert text in build_system_prompt(agent)

    def test_identity_falls_back_when_there_is_no_soul(self, monkeypatch):
        from agent.prompt_builder import DEFAULT_AGENT_IDENTITY

        # ``ensure_hermes_home()`` seeds a starter SOUL.md, so "no SOUL" has to
        # be stated at the loader rather than by deleting a file.
        monkeypatch.setattr(run_agent, "load_soul_md", lambda *a, **k: None)
        agent = _StubAgent()
        text, loaded = build_identity_block(agent, None)
        assert loaded is False
        assert text == DEFAULT_AGENT_IDENTITY

    def test_identity_respects_the_cron_gate(self, soul_md):
        """``skip_context_files`` without ``load_soul_identity`` = no SOUL."""
        soul_md()
        agent = _StubAgent(load_soul_identity=False, skip_context_files=True)
        text, loaded = build_identity_block(agent, None)
        assert loaded is False
        assert _MARKER not in text

    def test_profile_hint_is_in_the_canonical_prompt(self):
        agent = _StubAgent()
        hint = build_active_profile_hint()
        assert "Active Hermes profile" in hint
        assert hint in build_system_prompt(agent)

    def test_platform_hint_is_in_the_canonical_prompt(self):
        agent = _StubAgent(platform="telegram")
        hint = resolve_platform_hint_block(agent)
        assert hint
        assert hint in build_system_prompt(agent)

    def test_platform_hint_honours_a_config_override(self):
        agent = _StubAgent(
            platform="telegram",
            platform_hint_overrides={"telegram": {"append": "EXTRA-HINT-XYZ"}},
        )
        assert "EXTRA-HINT-XYZ" in resolve_platform_hint_block(agent)

    def test_context_length_extraction(self):
        class _CC:
            context_length = 200000

        assert prompt_context_length(_StubAgent()) is None
        assert prompt_context_length(_StubAgent(context_compressor=_CC())) == 200000


# ------------------------------------------------------------- the pure policy


class TestConfigResolution:
    """Every malformed value resolves to something usable. Nothing raises."""

    def test_the_default_is_hermes(self):
        cfg = resolve_system_prompt_config(None)
        assert cfg.mode == MODE_HERMES == DEFAULT_MODE
        assert cfg.sections == MODE_SECTIONS[MODE_HERMES]
        assert cfg.max_chars == DEFAULT_MAX_CHARS

    def test_absent_key_is_not_a_rejection(self, caplog):
        with caplog.at_level(logging.WARNING):
            cfg = resolve_system_prompt_config(None)
        assert cfg.mode == MODE_HERMES
        assert not [r for r in caplog.records if "system_prompt" in r.getMessage()]

    def test_configured_reads_the_key(self, write_prompt_config):
        write_prompt_config({"mode": "soul"})
        assert resolve_system_prompt_config(configured_system_prompt()).mode == MODE_SOUL

    def test_configured_survives_an_unreadable_config(self):
        from hermes_cli.config import get_config_path

        path = get_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("model: [not, a, mapping\n", encoding="utf-8")
        assert resolve_system_prompt_config(configured_system_prompt()).mode == MODE_HERMES

    @pytest.mark.parametrize("mode", [MODE_OFF, MODE_SOUL, MODE_HERMES, MODE_FULL])
    def test_known_modes_survive(self, mode):
        assert resolve_system_prompt_config({"mode": mode}).mode == mode

    @pytest.mark.parametrize("bad", ["nonsense", "HERMES", 3, [], {}])
    def test_unknown_mode_falls_back_and_warns(self, bad, caplog):
        with caplog.at_level(logging.WARNING):
            cfg = resolve_system_prompt_config({"mode": bad})
        assert cfg.mode == MODE_HERMES
        assert any("mode" in r.getMessage() for r in caplog.records)

    def test_off_is_the_only_value_that_sends_nothing(self):
        cfg = resolve_system_prompt_config({"mode": MODE_OFF})
        assert cfg.mode == MODE_OFF
        assert cfg.sections == ()

    def test_explicit_sections_are_an_allow_list(self):
        cfg = resolve_system_prompt_config({"sections": ["identity", "platform"]})
        assert cfg.sections == ("identity", "platform")

    def test_unknown_section_ids_are_dropped_and_named(self, caplog):
        with caplog.at_level(logging.WARNING):
            cfg = resolve_system_prompt_config(
                {"sections": ["identity", "nope", "also_nope"]}
            )
        assert cfg.sections == ("identity",)
        msg = " ".join(r.getMessage() for r in caplog.records)
        assert "nope" in msg and "also_nope" in msg

    def test_an_all_unknown_list_falls_back_to_the_mode_default(self, caplog):
        with caplog.at_level(logging.WARNING):
            cfg = resolve_system_prompt_config({"sections": ["nope"]})
        assert cfg.sections == MODE_SECTIONS[MODE_HERMES]

    def test_a_non_list_sections_value_warns_and_uses_the_mode_default(self, caplog):
        with caplog.at_level(logging.WARNING):
            cfg = resolve_system_prompt_config({"sections": "identity"})
        assert cfg.sections == MODE_SECTIONS[MODE_HERMES]
        assert any("sections" in r.getMessage() for r in caplog.records)

    def test_sections_under_full_are_ignored_and_warned(self, caplog):
        with caplog.at_level(logging.WARNING):
            cfg = resolve_system_prompt_config(
                {"mode": MODE_FULL, "sections": ["identity"]}
            )
        assert cfg.mode == MODE_FULL
        assert cfg.sections == ()
        assert any("full" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize(
        "raw,expected",
        [(10, 1000), (999, 1000), (1000, 1000), (5000, 5000),
         (120001, 120000), (10**9, 120000)],
    )
    def test_max_chars_is_clamped(self, raw, expected, caplog):
        with caplog.at_level(logging.WARNING):
            assert resolve_system_prompt_config({"max_chars": raw}).max_chars == expected

    @pytest.mark.parametrize("bad", ["lots", None, [], 3.5, True])
    def test_a_non_int_max_chars_uses_the_default(self, bad):
        assert resolve_system_prompt_config(
            {"max_chars": bad}
        ).max_chars == DEFAULT_MAX_CHARS

    def test_a_non_mapping_node_resolves_to_the_default(self, caplog):
        with caplog.at_level(logging.WARNING):
            cfg = resolve_system_prompt_config(["mode", "soul"])
        assert cfg.mode == MODE_HERMES

    def test_the_mode_default_sets_are_a_subset_of_the_vocabulary(self):
        for mode, ids in MODE_SECTIONS.items():
            assert set(ids) <= set(SECTIONS), mode

    def test_the_default_set_excludes_the_hermes_only_blocks(self):
        default = set(MODE_SECTIONS[MODE_HERMES])
        assert not (default & {"tool_guidance", "skills_index", "context_files", "coding"})

    def test_the_default_set_includes_the_hermes_operating_context(self):
        assert set(MODE_SECTIONS[MODE_HERMES]) == {
            "identity", "environment", "profile", "platform",
            "caller_instructions", "memory", "user_profile", "external_memory",
        }

    def test_soul_is_identity_only(self):
        assert MODE_SECTIONS[MODE_SOUL] == ("identity",)


# -------------------------------------------------------------- the composition


class TestComposition:
    """What each mode contains — and what it must never contain."""

    def _agent(self, **kw):
        kw.setdefault(
            "memory_store",
            _StubMemoryStore(
                memory="## Memory\nMEM-MARKER-aa", user="## User\nUSER-MARKER-bb"
            ),
        )
        kw.setdefault("memory_manager", _StubMemoryManager("EXT-MEM-MARKER-cc"))
        return _StubAgent(**kw)

    def test_soul_mode_is_identity_and_framing_only(self, soul_md):
        soul_md()
        text = compose_system_prompt(
            self._agent(), resolve_system_prompt_config({"mode": MODE_SOUL})
        )
        assert PREAMBLE in text
        assert _MARKER in text
        assert "MEM-MARKER-aa" not in text
        assert "USER-MARKER-bb" not in text

    def test_hermes_mode_carries_the_operating_context(self, soul_md):
        soul_md()
        agent = self._agent(
            platform="telegram", active_system_message="CALLER-MARKER-dd"
        )
        text = compose_system_prompt(agent, resolve_system_prompt_config(None))
        assert _MARKER in text
        assert "Active Hermes profile" in text
        assert "MEM-MARKER-aa" in text
        assert "USER-MARKER-bb" in text
        assert "EXT-MEM-MARKER-cc" in text
        assert "CALLER-MARKER-dd" in text
        assert resolve_platform_hint_block(agent) in text

    @pytest.mark.parametrize("mode", [MODE_SOUL, MODE_HERMES])
    def test_hermes_only_tool_guidance_never_ships_by_default(self, mode, soul_md):
        """The blocks that steer tools this session does not have."""
        from agent.prompt_builder import (
            MEMORY_GUIDANCE,
            PARALLEL_TOOL_CALL_GUIDANCE,
            SKILLS_GUIDANCE,
            STEER_CHANNEL_NOTE,
            TASK_COMPLETION_GUIDANCE,
        )

        soul_md()
        agent = self._agent(
            valid_tool_names=["memory", "skill_manage", "terminal", "read_file"]
        )
        text = compose_system_prompt(
            agent, resolve_system_prompt_config({"mode": mode})
        )
        for block in (MEMORY_GUIDANCE, SKILLS_GUIDANCE, STEER_CHANNEL_NOTE,
                      TASK_COMPLETION_GUIDANCE, PARALLEL_TOOL_CALL_GUIDANCE):
            assert block not in text

    def test_the_misleading_model_provider_line_never_ships(self, soul_md):
        soul_md()
        text = compose_system_prompt(self._agent(), resolve_system_prompt_config(None))
        assert "Provider: claude-code-acp" not in text
        assert "Model: claude-code" not in text

    def test_the_vocabulary_note_is_present_and_sorted(self, soul_md):
        from agent.claude_acp_skills import HERMES_TO_CLAUDE_TOOLS

        soul_md()
        note = vocabulary_note()
        assert note in compose_system_prompt(
            self._agent(), resolve_system_prompt_config(None)
        )
        # Order the names as they appear IN THE NOTE (not in the dict): a note
        # rendered straight from insertion order would change bytes whenever
        # the table gains an entry, and the digest with it.
        positions = sorted(
            (note.index(name), name)
            for name in HERMES_TO_CLAUDE_TOOLS
            if name in note
        )
        rendered = [name for _, name in positions]
        assert rendered == sorted(HERMES_TO_CLAUDE_TOOLS), (
            "the note must render every mapping, sorted, so it is byte-stable"
        )

    def test_full_mode_appends_the_cached_prompt_verbatim(self):
        agent = self._agent(cached_system_prompt="CACHED-PROMPT-BODY-ee")
        text = compose_system_prompt(
            agent, resolve_system_prompt_config({"mode": MODE_FULL})
        )
        assert text == "\n\n".join(
            [PREAMBLE, vocabulary_note(), "CACHED-PROMPT-BODY-ee"]
        )

    def test_off_mode_composes_nothing(self):
        assert compose_system_prompt(
            self._agent(), resolve_system_prompt_config({"mode": MODE_OFF})
        ) == ""

    def test_an_explicit_section_list_is_honoured(self, soul_md):
        soul_md()
        text = compose_system_prompt(
            self._agent(),
            resolve_system_prompt_config({"sections": ["identity", "memory"]}),
        )
        assert _MARKER in text and "MEM-MARKER-aa" in text
        assert "USER-MARKER-bb" not in text
        assert "Active Hermes profile" not in text

    def test_a_section_whose_source_raises_is_skipped_not_fatal(self, soul_md):
        class _Exploding:
            def format_for_system_prompt(self, target):
                raise RuntimeError("boom")

        soul_md()
        text = compose_system_prompt(
            self._agent(memory_store=_Exploding()),
            resolve_system_prompt_config(None),
        )
        assert _MARKER in text

    def test_composition_never_reaches_another_profiles_home(self, soul_md, tmp_path,
                                                             monkeypatch):
        """Two HERMES_HOMEs, two SOULs, no leakage — and no ``Path.home()``."""
        soul_md(f"You are Hermes. {_MARKER}")
        first = compose_system_prompt(self._agent(), resolve_system_prompt_config(None))

        other = tmp_path / "other-home"
        other.mkdir()
        (other / "SOUL.md").write_text("You are Hermes. OTHER-MARKER-ff", "utf-8")
        monkeypatch.setenv("HERMES_HOME", str(other))
        second = compose_system_prompt(self._agent(), resolve_system_prompt_config(None))

        assert _MARKER in first and "OTHER-MARKER-ff" not in first
        assert "OTHER-MARKER-ff" in second and _MARKER not in second


class TestBoundingAndDeterminism:
    def test_composing_twice_is_byte_identical(self, soul_md):
        soul_md()
        agent = _StubAgent()
        cfg = resolve_system_prompt_config(None)
        first = compose_system_prompt(agent, cfg)
        assert first == compose_system_prompt(agent, cfg)
        assert prompt_digest(first) == prompt_digest(first)

    def test_the_digest_is_a_sha256_of_the_text(self):
        assert prompt_digest("abc") == hashlib.sha256(b"abc").hexdigest()

    def test_an_oversized_composition_drops_sections_in_order(self, soul_md, caplog):
        soul_md(f"You are Hermes. {_MARKER}")
        agent = _StubAgent(
            memory_store=_StubMemoryStore(
                memory="M" * 4000, user="U" * 4000
            ),
            memory_manager=_StubMemoryManager("E" * 4000),
            active_system_message="C" * 4000,
        )
        cfg = resolve_system_prompt_config({"max_chars": 1000})
        with caplog.at_level(logging.WARNING):
            text = compose_system_prompt(agent, cfg)
        assert len(text) <= 1000
        assert _MARKER in text, "identity is never dropped"
        assert PREAMBLE in text or len(PREAMBLE) > 1000

    def test_dropping_is_deterministic_and_named(self, soul_md):
        soul_md()
        agent = _StubAgent(
            memory_store=_StubMemoryStore(memory="M" * 9000, user="U" * 9000),
        )
        cfg = resolve_system_prompt_config({"max_chars": 2000})
        first = compose_system_prompt(agent, cfg)
        assert first == compose_system_prompt(agent, cfg)
        assert "omitted" in first.lower()

    def test_the_drop_order_never_contains_identity(self):
        assert "identity" not in DROP_ORDER
        assert set(DROP_ORDER) == set(SECTIONS) - {"identity"}


class TestWireShape:
    """The object form, always. The string form deletes Claude Code's preset."""

    def test_meta_is_the_clamped_object_form(self):
        meta = system_prompt_meta("hello")
        assert meta == {
            "type": "preset",
            "preset": "claude_code",
            "append": "hello",
        }

    def test_meta_is_never_a_bare_string(self):
        assert not isinstance(system_prompt_meta("hello"), str)

    @pytest.mark.parametrize("empty", ["", "   ", None])
    def test_empty_text_sends_no_meta_at_all(self, empty):
        assert system_prompt_meta(empty) is None

    def test_compose_for_agent_is_fail_open(self, monkeypatch):
        import agent.claude_acp_system_prompt as mod

        monkeypatch.setattr(
            mod, "compose_system_prompt",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert compose_for_agent(_StubAgent()) is None

    def test_session_new_carries_the_meta(self, monkeypatch):
        from agent.transports.claude_acp import ClaudeAcpClient

        sent = {}

        class _Client(ClaudeAcpClient):
            def __init__(self):
                self._cwd = "/tmp"

            def request(self, method, params, timeout=None):
                sent[method] = params
                return {"sessionId": "s1"}

        _Client().session_new(system_prompt={"type": "preset", "preset": "claude_code",
                                             "append": "X"})
        assert sent["session/new"]["_meta"]["systemPrompt"]["append"] == "X"

    def test_session_new_without_a_prompt_carries_no_meta(self):
        from agent.transports.claude_acp import ClaudeAcpClient

        sent = {}

        class _Client(ClaudeAcpClient):
            def __init__(self):
                self._cwd = "/tmp"

            def request(self, method, params, timeout=None):
                sent[method] = params
                return {"sessionId": "s1"}

        _Client().session_new()
        assert "_meta" not in sent["session/new"]

    def test_session_load_carries_the_meta(self):
        from agent.transports.claude_acp import ClaudeAcpClient

        sent = {}

        class _Client(ClaudeAcpClient):
            def __init__(self):
                self._cwd = "/tmp"

            def _request_pumping(self, method, params, **kw):
                sent[method] = params
                return {}

        _Client().session_load("s1", system_prompt={"type": "preset",
                                                    "preset": "claude_code",
                                                    "append": "Y"})
        assert sent["session/load"]["_meta"]["systemPrompt"]["append"] == "Y"


# ------------------------------------------------------------------- the wire


class TestSystemPromptReachesTheSession:
    """REAL fake-adapter subprocesses over REAL stdio."""

    def test_system_prompt_reaches_session_new(self, tmp_path, monkeypatch, soul_md):
        soul_md()
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "normal", monkeypatch, record=record,
                            skip_context_files=False)
        try:
            agent.run_conversation("hello")
        finally:
            agent.close()

        params = _record_data(record)["session_new_params"][0]
        meta = params["_meta"]["systemPrompt"]
        assert meta["type"] == "preset" and meta["preset"] == "claude_code"
        assert _MARKER in meta["append"], (
            "the active profile's SOUL.md must reach the Claude Code session"
        )

    def test_the_agent_side_receives_it(self, tmp_path, monkeypatch, soul_md):
        soul_md()
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "memory", monkeypatch, record=record,
                            store=tmp_path / "store", skip_context_files=False,
                            FAKE_ACP_ECHO_SYSTEM_PROMPT="1")
        from agent.claude_code_runtime import claude_acp_system_prompt_digest

        try:
            result = agent.run_conversation("hello")
            # Read before close(): closing releases the session, and the
            # accessor reports what a live conversation was created with.
            digest = claude_acp_system_prompt_digest(agent)
        finally:
            agent.close()

        assert digest
        assert f"sysprompt[{digest[:8]}" in result["final_response"], (
            "the agent side must echo the digest of what it actually received"
        )

    def test_not_replayed_per_turn(self, tmp_path, monkeypatch, soul_md):
        soul_md()
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "memory", monkeypatch, record=record,
                            store=tmp_path / "store", skip_context_files=False)
        try:
            for turn in ("one", "two", "three"):
                agent.run_conversation(turn)
        finally:
            agent.close()

        data = _record_data(record)
        assert len(data["session_new_params"]) == 1
        assert len(data["prompts"]) == 3
        for prompt in data["prompts"]:
            blob = json.dumps(prompt)
            assert _MARKER not in blob, "the context must never be replayed per turn"
            assert "_meta" not in blob

    def test_resent_verbatim_on_rebind(self, tmp_path, monkeypatch, soul_md):
        soul_md()
        record_dir = tmp_path / "records"
        record_dir.mkdir()
        db = SessionDB(db_path=tmp_path / "sessions.db")
        agent = _agent_with(tmp_path, "memory", monkeypatch, record_dir=record_dir,
                            store=tmp_path / "store", db=db,
                            session_id="sess-rebind", skip_context_files=False)
        try:
            agent.run_conversation("first")
            pid = agent._claude_acp_session.adapter_pid
            _kill(pid)
            agent.run_conversation("second")
            second_pid = agent._claude_acp_session.adapter_pid
        finally:
            agent.close()

        records = []
        for path in sorted(record_dir.glob("*.json")):
            records.append(json.loads(path.read_text(encoding="utf-8")))
        new_appends = [
            _append_of(p) for r in records for p in r.get("session_new_params", [])
        ]
        load_appends = [
            _append_of(p) for r in records for p in r.get("session_load_params", [])
        ]
        assert new_appends and load_appends
        assert all(isinstance(a, str) and _MARKER in a
                   for a in new_appends + load_appends), (
            "both calls must carry a real append, not an absent _meta"
        )
        assert load_appends[0] == new_appends[0], (
            "a rebind must re-send byte-identical bytes"
        )
        assert second_pid != pid, (
            "session/load must always run against a freshly spawned adapter — "
            "the adapter's session fingerprint does not include _meta"
        )

    def test_pinned_across_a_soul_edit_within_one_session(
        self, tmp_path, monkeypatch, soul_md
    ):
        soul_md()
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "memory", monkeypatch, record=record,
                            store=tmp_path / "store", skip_context_files=False)
        try:
            agent.run_conversation("first")
            from agent.claude_code_runtime import claude_acp_system_prompt_digest

            before = claude_acp_system_prompt_digest(agent)
            soul_md("You are Hermes. TOTALLY-DIFFERENT-SOUL-gg")
            agent.run_conversation("second")
            after = claude_acp_system_prompt_digest(agent)
        finally:
            agent.close()

        assert before, "the first connect must have composed something"
        assert before == after, "the composition is memoized per ACP session object"
        data = _record_data(record)
        assert len(data["session_new_params"]) == 1

    def test_mode_off_sends_no_meta(self, tmp_path, monkeypatch, soul_md,
                                    write_prompt_config):
        soul_md()
        write_prompt_config({"mode": MODE_OFF})
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "normal", monkeypatch, record=record,
                            skip_context_files=False)
        try:
            result = agent.run_conversation("hello")
        finally:
            agent.close()

        assert "_meta" not in _record_data(record)["session_new_params"][0]
        assert result["completed"] is True

    def test_binding_records_the_digest_and_mode(self, tmp_path, monkeypatch, soul_md):
        soul_md()
        db = SessionDB(db_path=tmp_path / "sessions.db")
        agent = _agent_with(tmp_path, "normal", monkeypatch, db=db,
                            session_id="sess-binding", skip_context_files=False)
        try:
            agent.run_conversation("hello")
            from agent.claude_code_runtime import (
                claude_acp_system_prompt_digest,
                claude_acp_system_prompt_text,
            )

            digest = claude_acp_system_prompt_digest(agent)
            text = claude_acp_system_prompt_text(agent)
        finally:
            agent.close()

        binding = db.get_session_model_config_value("sess-binding", "claude_acp")
        assert binding["system_prompt_sha256"] == digest == prompt_digest(text)
        assert binding["system_prompt_mode"] == MODE_HERMES

    def test_composition_failure_is_fail_open(self, tmp_path, monkeypatch, soul_md,
                                              caplog):
        soul_md()
        record = tmp_path / "record.json"
        import agent.claude_acp_system_prompt as mod

        monkeypatch.setattr(
            mod, "compose_system_prompt",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        agent = _agent_with(tmp_path, "normal", monkeypatch, record=record,
                            skip_context_files=False)
        try:
            with caplog.at_level(logging.WARNING):
                result = agent.run_conversation("hello")
        finally:
            agent.close()

        assert result["completed"] is True
        assert "_meta" not in _record_data(record)["session_new_params"][0]

    def test_no_prompt_text_in_logs(self, tmp_path, monkeypatch, soul_md, caplog):
        soul_md()
        record = tmp_path / "record.json"
        agent = _agent_with(tmp_path, "normal", monkeypatch, record=record,
                            skip_context_files=False)
        try:
            with caplog.at_level(logging.DEBUG):
                agent.run_conversation("hello")
        finally:
            agent.close()

        assert _MARKER in _append_of(_record_data(record)["session_new_params"][0])
        for rec in caplog.records:
            assert _MARKER not in rec.getMessage()

    def test_the_transcript_and_role_alternation_are_unchanged(
        self, tmp_path, monkeypatch, soul_md, write_prompt_config
    ):
        """Nothing this packet sends appears in the conversation.

        Asserted as a comparison rather than as a frozen row list: the claim is
        "``mode: off`` and the default produce the same transcript", and a
        literal expected shape would only pin whatever the fake adapter emits
        today.
        """
        soul_md()
        db = SessionDB(db_path=tmp_path / "sessions.db")

        def _run(session_id):
            agent = _agent_with(tmp_path / session_id, "tools", monkeypatch, db=db,
                                session_id=session_id, skip_context_files=False)
            (tmp_path / session_id).mkdir(exist_ok=True)
            try:
                return agent.run_conversation("hello")
            finally:
                agent.close()

        write_prompt_config({"mode": MODE_OFF})
        without = _run("sess-roles-off")
        write_prompt_config({"mode": MODE_HERMES})
        with_context = _run("sess-roles-on")

        for result, session_id in ((without, "sess-roles-off"),
                                   (with_context, "sess-roles-on")):
            for roles in ([row["role"] for row in db.get_messages(session_id)],
                          [m["role"] for m in result["messages"]]):
                assert roles[0] == "user" and roles[-1] == "assistant"
                assert not [a for a, b in zip(roles, roles[1:]) if a == b], (
                    "no two same-role messages in a row"
                )

        assert (
            [m["role"] for m in with_context["messages"]]
            == [m["role"] for m in without["messages"]]
        ), "the operating context rides on session/new, never in the transcript"
        assert with_context["agent_persisted"] == without["agent_persisted"]

    def test_the_session_is_not_retired_by_the_prompt(self, tmp_path, monkeypatch,
                                                      soul_md):
        """D3: ``matches()`` deliberately excludes the composed prompt."""
        from agent.claude_acp_session import ClaudeAcpSession

        session = ClaudeAcpSession(argv=["x"], cwd=str(tmp_path),
                                   system_prompt_provider=lambda: "anything")
        try:
            assert session.matches(["x"], str(tmp_path))
        finally:
            session.close()

    @pytest.mark.parametrize("value", ["a bare string", 42, object()])
    def test_an_unusable_provider_answer_is_fail_open(self, tmp_path, value, caplog):
        """A provider answering with something unsendable must not reach
        ``composed.meta()`` inside ``_connect`` — that would abort the turn."""
        from agent.claude_acp_session import ClaudeAcpSession

        session = ClaudeAcpSession(argv=["x"], cwd=str(tmp_path),
                                   system_prompt_provider=lambda: value)
        try:
            with caplog.at_level(logging.WARNING):
                assert session._compose_system_prompt() is None
        finally:
            session.close()
        assert any("unusable" in r.getMessage() for r in caplog.records)

    def test_a_raising_provider_is_memoized_not_retried(self, tmp_path):
        """The memo covers the ``None`` answer too: a respawn must not compose
        different bytes for the same conversation."""
        from agent.claude_acp_session import ClaudeAcpSession

        calls = []

        def _boom():
            calls.append(1)
            raise RuntimeError("boom")

        session = ClaudeAcpSession(argv=["x"], cwd=str(tmp_path),
                                   system_prompt_provider=_boom)
        try:
            assert session._compose_system_prompt() is None
            assert session._compose_system_prompt() is None
        finally:
            session.close()
        assert len(calls) == 1
