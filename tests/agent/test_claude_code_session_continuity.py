"""Packet 2 — session continuity for the Claude Code ACP runtime.

One Hermes conversation keeps ONE Claude Code session across turns and
survives an adapter restart, with Hermes staying authoritative: the ACP
session id is a discardable binding on the Hermes session row, and there is no
second journal anywhere.

Every test drives real fake-adapter subprocesses over real stdio. The failure
modes at stake here — a second adapter per session, replayed agent-side
history landing in the transcript, a lease held by a process that died — do
not reproduce against mocks.
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
from hermes_state import SessionDB

_FAKE_ADAPTER = os.path.join(os.path.dirname(__file__), "_fake_claude_acp_adapter.py")


# --------------------------------------------------------------------- helpers


@pytest.fixture
def acp(tmp_path, monkeypatch):
    """Adapter-side store + per-process record dir + an isolated workspace."""
    store = tmp_path / "acp-store"
    records = tmp_path / "acp-records"
    workdir = tmp_path / "workspace"
    for path in (store, records, workdir):
        path.mkdir()
    monkeypatch.setenv("FAKE_ACP_MODE", "memory")
    monkeypatch.setenv("FAKE_ACP_STORE", str(store))
    monkeypatch.setenv("FAKE_ACP_RECORD_DIR", str(records))

    class _Env:
        def __init__(self) -> None:
            self.store = store
            self.records = records
            self.workdir = workdir
            self.db = SessionDB(db_path=tmp_path / "sessions.db")
            self._history: dict[int, list] = {}

        def agent(self, session_id: str = "sess-acp"):
            a = run_agent.AIAgent(
                api_key="claude-code-acp",
                base_url="acp://claude-code",
                provider="claude-code-acp",
                api_mode="claude_code_acp",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                session_db=self.db,
                session_id=session_id,
            )
            a.acp_command = sys.executable
            a.acp_args = [_FAKE_ADAPTER]
            a.session_cwd = str(self.workdir)
            # The post-turn background review forks its own agent (and, on
            # this runtime, its own adapter). Packet 1 owns that behaviour;
            # here it would only add adapters this suite is trying to count.
            a._skill_nudge_interval = 0
            return a

        def talk(self, agent, text: str):
            """One turn, threading the transcript the way real surfaces do.

            The CLI and the gateway both carry the conversation forward via
            ``conversation_history``; a recap built from Hermes' transcript is
            only meaningful when the caller behaves that way.
            """
            history = self._history.setdefault(id(agent), [])
            result = agent.run_conversation(text, conversation_history=list(history))
            self._history[id(agent)] = list(result["messages"])
            return result

        def adapters(self) -> list[dict]:
            """Every adapter process's record, oldest first."""
            out = []
            for path in sorted(self.records.glob("*.json"), key=lambda p: p.stat().st_mtime):
                try:
                    out.append(json.loads(path.read_text(encoding="utf-8")))
                except Exception:
                    continue
            return out

        def binding(self, session_id: str = "sess-acp"):
            from agent.claude_acp_binding import BINDING_KEY

            return self.db.get_session_model_config_value(session_id, BINDING_KEY)

    env = _Env()
    yield env
    # Never leave an adapter (or its process tree) behind for the next test.
    try:
        from agent.claude_acp_session import close_all_sessions

        close_all_sessions()
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    # psutil, not os.kill(pid, 0): on Windows the latter signals CTRL_C_EVENT
    # to the target's console group rather than probing liveness (bpo-14484).
    # A zombie counts as dead: an adapter we killed stays in the table until
    # its Popen owner waits on it, and it holds nothing open in the meantime.
    import psutil

    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except Exception:
        return psutil.pid_exists(pid)


def _wait_dead(pid: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.05)
    assert not _pid_alive(pid), f"adapter pid {pid} survived"


def _kill(pid: int) -> None:
    import psutil

    try:
        psutil.Process(pid).kill()
    except Exception:
        pass
    _wait_dead(pid)


def _prompt_blocks(record: dict, index: int = -1) -> list[dict]:
    return record["prompts"][index]["prompt"]


def _blocks_text(record: dict, index: int = -1) -> str:
    return " ".join(b.get("text", "") for b in _prompt_blocks(record, index))


# ------------------------------------------------------------ adapter reuse


class TestOneAdapterPerSession:
    def test_two_turns_share_one_adapter_and_one_acp_session(self, acp):
        a = acp.agent()
        acp.talk(a, "q-one")
        acp.talk(a, "q-two")

        adapters = acp.adapters()
        assert len(adapters) == 1, "each turn spawned its own adapter"
        assert len(adapters[0]["session_new_calls"]) == 1
        assert len(adapters[0]["prompts"]) == 2

    def test_turn_two_recalls_turn_one(self, acp):
        a = acp.agent()
        acp.talk(a, "the-magic-word-is-plover")
        second = acp.talk(a, "what was the magic word?")
        assert "the-magic-word-is-plover" in second["final_response"]

    def test_a_second_agent_on_the_same_session_retires_the_first_adapter(self, acp):
        first = acp.agent()
        acp.talk(first, "q-one")
        first_pid = acp.adapters()[0]["adapter_pid"]

        second = acp.agent()  # same session id, e.g. a gateway agent-cache rebuild
        acp.talk(second, "q-two")

        _wait_dead(first_pid)
        alive = [r["adapter_pid"] for r in acp.adapters() if _pid_alive(r["adapter_pid"])]
        assert len(alive) == 1

    def test_close_reaps_the_adapter(self, acp):
        a = acp.agent()
        acp.talk(a, "q-one")
        pid = acp.adapters()[0]["adapter_pid"]
        assert _pid_alive(pid)

        a.close()
        _wait_dead(pid)


# ------------------------------------------------------------------- binding


class TestDiscardableBinding:
    def test_acp_session_id_is_persisted_on_the_hermes_session_row(self, acp):
        a = acp.agent()
        acp.talk(a, "q-one")

        created = acp.adapters()[0]["session_new_calls"][0]
        binding = acp.binding()
        assert binding, "no ACP binding persisted on the Hermes session"
        assert binding["acp_session_id"] == created
        assert binding["cwd"] == str(acp.workdir)

    def test_no_second_journal_is_written(self, acp):
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
        before = {str(p) for p in home.rglob("*")} if home.exists() else set()

        a = acp.agent()
        acp.talk(a, "q-one")
        acp.talk(a, "q-two")

        after = {str(p) for p in home.rglob("*")} if home.exists() else set()
        new = [p for p in after - before if "acp" in os.path.basename(p).lower()]
        assert not new, f"ACP state was written outside the session store: {new}"

    def test_binding_is_discarded_when_the_workspace_changed(self, acp):
        a = acp.agent()
        acp.talk(a, "q-one")
        first_session = acp.binding()["acp_session_id"]

        moved = acp.workdir.parent / "workspace-2"
        moved.mkdir()
        b = acp.agent()
        b.session_cwd = str(moved)
        acp.talk(b, "q-two")

        assert acp.binding()["acp_session_id"] != first_session
        assert acp.binding()["cwd"] == str(moved)


# --------------------------------------------------------- restart / rebind


class TestAdapterRestart:
    def test_killing_the_adapter_rebinds_via_session_load(self, acp):
        a = acp.agent()
        acp.talk(a, "the-magic-word-is-plover")
        bound = acp.binding()["acp_session_id"]
        _kill(acp.adapters()[0]["adapter_pid"])

        second = acp.talk(a, "what was the magic word?")

        adapters = acp.adapters()
        assert len(adapters) == 2
        assert adapters[1]["session_load_calls"] == [bound]
        assert not adapters[1]["session_new_calls"]
        assert "the-magic-word-is-plover" in second["final_response"]

    def test_a_fresh_process_rebinds_from_the_persisted_binding(self, acp):
        first = acp.agent()
        acp.talk(first, "the-magic-word-is-plover")
        bound = acp.binding()["acp_session_id"]
        first.close()

        # A brand-new AIAgent with no in-memory state — only the Hermes
        # session row can carry the binding across.
        second = acp.agent()
        answer = acp.talk(second, "what was the magic word?")

        assert acp.adapters()[1]["session_load_calls"] == [bound]
        assert "the-magic-word-is-plover" in answer["final_response"]

    def test_permission_mode_is_renegotiated_after_a_rebind(self, acp):
        a = acp.agent()
        acp.talk(a, "q-one")
        _kill(acp.adapters()[0]["adapter_pid"])
        acp.talk(a, "q-two")

        assert acp.adapters()[1]["set_mode_calls"] == ["default"]

    def test_replayed_history_never_reaches_the_response_or_transcript(self, acp):
        a = acp.agent()
        acp.talk(a, "q-one")
        _kill(acp.adapters()[0]["adapter_pid"])

        result = acp.talk(a, "q-two")

        assert "REPLAYED-HISTORY" not in result["final_response"]
        serialized = json.dumps(result["messages"])
        assert "REPLAYED-HISTORY" not in serialized

    def test_a_late_replay_notification_cannot_leak_into_the_next_turn(
        self, acp, monkeypatch
    ):
        a = acp.agent()
        acp.talk(a, "q-one")
        _kill(acp.adapters()[0]["adapter_pid"])
        monkeypatch.setenv("FAKE_ACP_MODE", "load-replays-late")

        result = acp.talk(a, "q-two")

        assert "REPLAYED-HISTORY" not in result["final_response"]


# ------------------------------------------------------------ reseed fallback


class TestReseedFallback:
    def _reseed(self, acp, mode: str, monkeypatch):
        a = acp.agent()
        acp.talk(a, "the-magic-word-is-plover")
        _kill(acp.adapters()[0]["adapter_pid"])
        monkeypatch.setenv("FAKE_ACP_MODE", mode)
        return a, acp.talk(a, "what was the magic word?")

    @pytest.mark.parametrize("mode", ["no-load", "load-fails"])
    def test_fallback_starts_a_new_session_with_exactly_one_marker(
        self, acp, monkeypatch, mode
    ):
        from agent.claude_code_runtime import RESEED_MARKER

        a, result = self._reseed(acp, mode, monkeypatch)

        adapters = acp.adapters()
        assert len(adapters[1]["session_new_calls"]) == 1
        assert result["final_response"].count(RESEED_MARKER) == 1

    def test_fallback_sends_a_bounded_recap_not_the_transcript(self, acp, monkeypatch):
        # Seed with the plain adapter: its replies do not echo earlier
        # questions back, so what lands in the recap is unambiguous.
        monkeypatch.setenv("FAKE_ACP_MODE", "normal")
        a = acp.agent()
        for i in range(12):
            acp.talk(a, f"question-number-{i}-" + "x" * 400)
        _kill(acp.adapters()[0]["adapter_pid"])
        monkeypatch.setenv("FAKE_ACP_MODE", "no-load")

        acp.talk(a, "what now?")

        sent = _blocks_text(acp.adapters()[1])
        assert "question-number-11" in sent, "recap dropped the most recent turn"
        assert "question-number-0" not in sent, "recap replayed the whole transcript"
        assert len(sent) < 8000, f"recap is unbounded ({len(sent)} chars)"

    def test_the_marker_appears_once_and_not_on_later_turns(self, acp, monkeypatch):
        from agent.claude_code_runtime import RESEED_MARKER

        a, first = self._reseed(acp, "no-load", monkeypatch)
        second = acp.talk(a, "and now?")

        assert RESEED_MARKER in first["final_response"]
        assert RESEED_MARKER not in second["final_response"]

    def test_reseed_does_not_rebuild_the_system_prompt(self, acp, monkeypatch):
        a = acp.agent()
        acp.talk(a, "q-one")
        before = a._cached_system_prompt
        _kill(acp.adapters()[0]["adapter_pid"])
        monkeypatch.setenv("FAKE_ACP_MODE", "no-load")

        acp.talk(a, "q-two")

        assert a._cached_system_prompt is before

    def test_role_alternation_survives_a_reseed(self, acp, monkeypatch):
        a, result = self._reseed(acp, "no-load", monkeypatch)
        roles = [m["role"] for m in result["messages"] if m["role"] in ("user", "assistant")]
        assert roles == ["user", "assistant", "user", "assistant"]

    def test_the_binding_is_rewritten_to_the_new_session(self, acp, monkeypatch):
        a = acp.agent()
        acp.talk(a, "q-one")
        old = acp.binding()["acp_session_id"]
        _kill(acp.adapters()[0]["adapter_pid"])
        monkeypatch.setenv("FAKE_ACP_MODE", "no-load")

        acp.talk(a, "q-two")

        assert acp.binding()["acp_session_id"] != old
        assert acp.binding()["acp_session_id"] == acp.adapters()[1]["session_new_calls"][0]


# ------------------------------------------------------------- idle eviction


class TestIdleEviction:
    def test_an_idle_adapter_is_evicted_and_the_next_turn_respawns(self, acp, monkeypatch):
        import agent.claude_acp_session as session_mod

        monkeypatch.setattr(session_mod, "IDLE_TTL_SECONDS", 0.3)
        a = acp.agent()
        acp.talk(a, "the-magic-word-is-plover")
        first_pid = acp.adapters()[0]["adapter_pid"]

        time.sleep(0.6)
        answer = acp.talk(a, "what was the magic word?")

        _wait_dead(first_pid)
        adapters = acp.adapters()
        assert len(adapters) == 2
        assert adapters[1]["session_load_calls"], "respawn did not rebind the session"
        assert "the-magic-word-is-plover" in answer["final_response"]


# ------------------------------------------------------------------ retirement


class TestWedgeRetirement:
    def test_a_wedged_turn_retires_the_adapter_and_the_next_turn_works(
        self, acp, monkeypatch
    ):
        import agent.claude_acp_session as session_mod

        monkeypatch.setattr(session_mod, "TURN_TIMEOUT_SECONDS", 2.0)
        monkeypatch.setenv("FAKE_ACP_MODE", "hang")
        a = acp.agent()

        wedged = acp.talk(a, "q-one")

        assert wedged["completed"] is False
        wedged_pid = acp.adapters()[0]["adapter_pid"]
        _wait_dead(wedged_pid)

        monkeypatch.setenv("FAKE_ACP_MODE", "memory")
        healthy = acp.talk(a, "q-two")
        assert healthy["completed"] is True
        assert len(acp.adapters()) == 2


# ----------------------------------------------------------------- turn lease


class TestTurnLease:
    def test_the_lease_is_held_during_a_turn_and_released_after(self, acp, tmp_path, monkeypatch):
        gate = tmp_path / "gate"
        monkeypatch.setenv("FAKE_ACP_MODE", "memory-gated")
        monkeypatch.setenv("FAKE_ACP_GATE", str(gate))
        a = acp.agent()

        done = threading.Event()

        def _turn():
            try:
                acp.talk(a, "q-one")
            finally:
                done.set()

        threading.Thread(target=_turn, daemon=True).start()
        deadline = time.time() + 20
        held = None
        while time.time() < deadline and held is None:
            binding = acp.binding() or {}
            held = binding.get("lease")
            time.sleep(0.05)
        assert held, "no turn lease was recorded while the turn was in flight"
        assert held["pid"] == os.getpid()

        gate.write_text("go", encoding="utf-8")
        assert done.wait(30)
        assert not (acp.binding() or {}).get("lease"), "lease survived the turn"

    def test_the_lease_is_released_after_an_adapter_error(self, acp, monkeypatch):
        monkeypatch.setenv("FAKE_ACP_MODE", "error")
        a = acp.agent()

        result = acp.talk(a, "q-one")

        assert result["completed"] is False
        assert not (acp.binding() or {}).get("lease")

    def test_the_lease_is_released_after_a_cancel(self, acp, monkeypatch):
        monkeypatch.setenv("FAKE_ACP_MODE", "slow")
        a = acp.agent()

        def _interrupt():
            # Wait for the adapter to actually hold the prompt. A fixed sleep
            # races turn setup and would measure that race, not the cancel
            # path (the same fix Packet 1's cancel tests needed).
            deadline = time.time() + 30
            while time.time() < deadline:
                if any(r.get("prompts") for r in acp.adapters()):
                    break
                time.sleep(0.02)
            a.interrupt("stop please")

        threading.Thread(target=_interrupt, daemon=True).start()
        result = acp.talk(a, "count forever")

        assert result["interrupted"] is True
        assert not (acp.binding() or {}).get("lease")

    def test_a_lease_left_by_a_dead_process_is_reclaimed(self, acp):
        a = acp.agent()
        acp.talk(a, "q-one")

        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        from agent.claude_acp_binding import BINDING_KEY

        binding = dict(acp.binding())
        binding["lease"] = {"holder": "crashed", "pid": dead.pid, "started_at": time.time()}
        acp.db.patch_session_model_config("sess-acp", {BINDING_KEY: binding})

        result = acp.talk(a, "q-two")

        assert result["completed"] is True
        assert not (acp.binding() or {}).get("lease")

    def test_a_lease_held_by_a_live_process_refuses_the_turn(self, acp):
        a = acp.agent()
        acp.talk(a, "q-one")
        adapters_before = len(acp.adapters())

        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            from agent.claude_acp_binding import BINDING_KEY

            binding = dict(acp.binding())
            binding["lease"] = {
                "holder": "another-hermes",
                "pid": holder.pid,
                "started_at": time.time(),
            }
            acp.db.patch_session_model_config("sess-acp", {BINDING_KEY: binding})

            result = acp.talk(a, "q-two")
        finally:
            holder.kill()
            holder.wait()

        assert result["completed"] is False
        assert "another turn" in result["final_response"].lower()
        assert len(acp.adapters()) == adapters_before, "a second adapter was spawned"

    def test_recovery_after_a_crash_does_not_double_deliver(self, acp):
        a = acp.agent()
        first = acp.talk(a, "q-one")
        assert first["completed"] is True

        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        from agent.claude_acp_binding import BINDING_KEY

        binding = dict(acp.binding())
        binding["lease"] = {"holder": "crashed", "pid": dead.pid, "started_at": time.time()}
        acp.db.patch_session_model_config("sess-acp", {BINDING_KEY: binding})

        second = acp.talk(a, "q-two")

        roles = [m["role"] for m in second["messages"] if m["role"] in ("user", "assistant")]
        assert roles == ["user", "assistant", "user", "assistant"]


# ------------------------------------------------------------ fork isolation


class TestForkIsolation:
    """The background-review fork shares the parent's session id by design.

    It is persistence-isolated, so it must not touch the parent's row — and on
    this runtime that means it must not steal the parent's ACP binding, its
    turn lease, or its adapter.
    """

    def test_a_persistence_isolated_fork_leaves_the_session_alone(self, acp):
        a = acp.agent()
        acp.talk(a, "q-one")
        bound = acp.binding()["acp_session_id"]
        main_pid = a._claude_acp_session.adapter_pid

        fork = acp.agent()  # same session id, as the review fork is
        fork._persist_disabled = True
        fork.run_conversation("review this conversation")

        assert acp.binding()["acp_session_id"] == bound
        assert not (acp.binding() or {}).get("lease")
        assert a._claude_acp_session.adapter_pid == main_pid
        assert _pid_alive(main_pid), "the fork retired the conversation's adapter"

        fork_pids = [
            r["adapter_pid"] for r in acp.adapters() if r["adapter_pid"] != main_pid
        ]
        assert fork_pids, "the fork did not run its own adapter"
        for pid in fork_pids:
            _wait_dead(pid)


# --------------------------------------------------------- security carry-over


class TestPacketOneSecurityStillHolds:
    def test_permissions_are_denied_on_the_second_turn_of_a_live_session(
        self, acp, monkeypatch
    ):
        monkeypatch.setenv("FAKE_ACP_MODE", "permission")
        a = acp.agent()
        acp.talk(a, "q-one")
        acp.talk(a, "q-two")

        record = acp.adapters()[0]
        assert record["wrote_file"] is False
        assert record["permission_outcome"]["outcome"] != "selected" or record[
            "permission_outcome"
        ].get("optionId") == "deny"
        assert not (acp.workdir / "permission-probe.txt").exists()

    def test_billing_credentials_are_still_stripped_after_a_respawn(
        self, acp, monkeypatch
    ):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-OBVIOUS-TEST-SENTINEL")
        a = acp.agent()
        acp.talk(a, "q-one")
        _kill(acp.adapters()[0]["adapter_pid"])
        acp.talk(a, "q-two")

        for record in acp.adapters():
            assert record["inherited_env"]["ANTHROPIC_API_KEY"] is None
