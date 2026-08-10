# Packet 2 report — Claude Code ACP session continuity

**Date:** 2026-08-10
**Branch:** `feat/claude-code-acp-runtime`
**Status:** Packet 2 complete. Packet 3 not started.
**Plan:** [`claude-code-acp-runtime-plan.md`](claude-code-acp-runtime-plan.md)
**Packet 1 report:** [`2026-08-09-claude-code-acp-packet1-report.md`](2026-08-09-claude-code-acp-packet1-report.md)

Nothing committed, pushed, or deployed. No user config edited — every run used
the isolated temporary `HERMES_HOME` the test harness provides.

---

## 1. What Packet 2 delivers

One Hermes conversation now keeps **one** Claude Code session across turns and
survives the adapter dying, while Hermes stays authoritative.

* **Durable binding, no second journal.** The ACP session id is one key
  (`claude_acp`) inside the existing `sessions.model_config` JSON — the same
  column that already carries `browser_model_lock`, `yolo_mode` and
  `_delegate_from`, written through the existing
  `patch_session_model_config` / `get_session_model_config_value` pair. No new
  table, no new file, no new state store.
* **One live adapter per active Hermes session,** reused across turns, keyed
  in a process registry so a rebuilt agent (gateway cache eviction, `/resume`
  in a new process) retires the previous adapter instead of racing it.
* **Capability-gated rebind.** When the adapter is gone, the next turn
  reattaches with `session/load` — only when the agent advertised
  `loadSession`. History that `session/load` replays is drained, never
  projected.
* **Reseed fallback.** If load is unavailable *or* fails, the turn starts a
  fresh session, sends a bounded recap built from Hermes' own transcript, and
  emits **exactly one** visible marker. No system-prompt rebuild, no full
  transcript replay.
* **Idle eviction and wedge retirement.** An idle adapter is evicted at the
  next turn (default 30 min) and a failed/wedged turn drops its adapter, both
  with the Packet 1 process-tree teardown. An interpreter-exit sweep reaps
  whatever is still live — necessary now that adapters outlive turns and lead
  their own process group.
* **Turn lease.** Acquired before anything is spawned and released in a
  `finally` on success, error, cancel, lease conflict, and auth failure. A
  lease left behind by a dead process is reclaimed; a lease held by a *live*
  foreign process refuses the turn instead of starting a second adapter
  against the same Claude Code session.

Packet 1's security posture is unchanged and re-applied on every rebind:
`session/set_mode` → `default` before any prompt, every
`session/request_permission` denied, API-billing credentials stripped while
`CLAUDE_CODE_OAUTH_TOKEN` is preserved, bounded/redacted stderr diagnostics,
role alternation intact.

## 2. Design decisions worth naming

**The binding is discardable, and the code treats it that way.** Every read is
tolerant (`{}` on any failure) and every write failure is logged, never
raised — losing the binding costs a reseed, never a message. The workspace is
part of the binding's identity: `session/load` reattaches a session to a `cwd`,
so a binding recorded elsewhere is discarded rather than resumed against the
wrong workspace.

**The lease is an advisory guard, not a distributed mutex.** It is a
read-modify-write on the session row, keyed on the same pid + process-create-
time liveness pair `hermes_cli/active_sessions.py` uses for its cross-process
session leases (both helpers are reused directly rather than reimplemented).
Two processes racing the same row in the same instant can both see "free".
Hermes runs one live agent per session, so the realistic failure — a *stale*
lease after a crash — is the one it handles exactly. Stated as R3 below rather
than papered over.

**Replayed history cannot leak into a turn.** `session/load` makes the agent
replay the transcript as notifications. Hermes owns the transcript, so those
updates are dropped twice over: the load call passes no update handler, and
the inbox is drained after the load reply and again immediately before every
prompt. On one ordered stdio stream this is exact — anything the agent emitted
before our prompt request is in the inbox by then, because the intervening
`session/set_mode` round-trip cannot overtake it. The fake adapter's
`load-replays-late` mode emits history *after* its own load reply to prove it.

**Cancellation state is per-turn, not per-session.** Each turn publishes a
fresh `_TurnState`; between turns there is nothing to cancel, so a stop that
arrives while nothing runs cannot silently abort the *next* turn. An interrupt
latched before a turn starts is carried in explicitly as `cancel_pending`,
preserving Packet 1's fix, and the turn state is published **before** the lease
write so a stop landing during that write is not dropped.

**The adapter's lifetime moved from the turn to the session — on purpose.**
Two Packet 1 tests asserted "no adapter survives the turn", which is exactly
the invariant this packet replaces. They were rewritten to assert the same
no-orphan property at the boundary that now owns teardown
(`agent.close()`), including the grandchild process-tree case. That is a
contract change, not a weakened assertion: the error path still retires the
adapter within the turn, and `test_no_orphan_after_an_adapter_error` is
untouched.

## 3. A real defect found on the way: the background-review fork

`_run_improvement_hooks` fires `_spawn_background_review`, which forks an
`AIAgent` that **inherits the parent's `api_mode` and is pinned to the
parent's `session_id`** (for prompt-cache reasons) while being
persistence-isolated. Under Packet 1 that only meant an extra short-lived
adapter. Under Packet 2 it was actively destructive: the fork's
`BindingStore` would have targeted the parent's session row and its session
registry entry would have retired the user's live adapter mid-conversation.

Fixed by giving persistence-isolated forks the isolation they already have
everywhere else (this mirrors `_get_session_db_for_recall`, which refuses to
open the canonical DB for exactly the same reason):

* `BindingStore.for_agent()` returns a non-durable store for `_persist_disabled`
  agents — a fork never writes the parent's row.
* the fork's session registers under its own anonymous key, so it cannot
  retire the conversation's adapter.
* the fork's adapter is closed at the end of its single turn.

`TestForkIsolation::test_a_persistence_isolated_fork_leaves_the_session_alone`
fails without those guards with `assert None == <parent adapter pid>` — the
fork had reaped the conversation's adapter.

**Not fixed, and carried as R1:** the review fork is *structurally unable to do
its job* on this runtime. The delegated path never sends Hermes tool schemas,
so a fork on `claude_code_acp` cannot call `skill_manage` or `memory` — it
spends a real Claude Code turn (and subscription quota) producing text nobody
consumes. `codex_app_server` avoids this by downgrading the fork to
`codex_responses`; Claude Code has no non-ACP sibling to downgrade to. Packet 1
took this hook as deliberate parity (F8), so reversing it is a Packet 1
decision, not a Packet 2 one. Recommendation in §7.

## 4. Changed files

### New (3)

| File | Purpose |
|---|---|
| `agent/claude_acp_binding.py` | Discardable ACP-session binding + turn lease on the Hermes session row |
| `agent/claude_acp_session.py` | One long-lived Claude Code session per conversation: reuse, rebind, reseed, idle eviction, retirement, cancellation, teardown registry |
| `tests/agent/test_claude_code_session_continuity.py` | 30 acceptance tests, all against real adapter subprocesses |

### Modified (6)

| File | Change |
|---|---|
| `agent/claude_code_runtime.py` | Per-turn `ClaudeAcpTurn` replaced by the session seam; bounded recap builder; reseed marker; lease-conflict failure path; fork isolation |
| `agent/transports/claude_acp.py` | `is_alive()` (reuse across turns needs a liveness answer) and `drain_inbox()` (replay tail must not become the next turn's output) |
| `run_agent.py` | Interrupt routes to `_claude_acp_session`; `close()` reaps the ACP session (new 6d block beside codex's 6c) |
| `tests/agent/_fake_claude_acp_adapter.py` | Agent-side session store that survives the process, `session/load` with history replay, per-process record files, and 6 new modes (`memory`, `memory-gated`, `hang`, `no-load`, `load-fails`, `load-replays-late`) |
| `tests/agent/test_claude_code_runtime.py` | Two reaping assertions moved from turn end to session close (see §2) |
| `tests/agent/test_claude_code_runtime_safety.py` | Same for the grandchild process-tree test; the unknown-request test now constructs `ClaudeAcpSession`; the late-cancel second-turn test no longer expects a mode switch a reused adapter cannot see |

Docs: this report + plan status/seam-table/Packet 2 section.

## 5. RED → GREEN

**RED**, with the acceptance suite written and no implementation:

```bash
scripts/run_tests.sh tests/agent/test_claude_code_session_continuity.py -q
# => 22 failed, 7 passed
```

The 7 that passed are negative guards that hold trivially in a per-turn world
(nothing is ever replayed if nothing is ever reloaded); every positive
continuity assertion failed. That run predates `TestForkIsolation`, which was
written later against the defect in §3 and has its own RED recorded there.
Representative failures, one per acceptance class:

```
FAILED …TestOneAdapterPerSession::test_two_turns_share_one_adapter_and_one_acp_session
FAILED …TestOneAdapterPerSession::test_turn_two_recalls_turn_one
FAILED …TestDiscardableBinding::test_acp_session_id_is_persisted_on_the_hermes_session_row
FAILED …TestAdapterRestart::test_killing_the_adapter_rebinds_via_session_load
FAILED …TestAdapterRestart::test_a_fresh_process_rebinds_from_the_persisted_binding
FAILED …TestReseedFallback::test_fallback_starts_a_new_session_with_exactly_one_marker[no-load]
FAILED …TestReseedFallback::test_fallback_starts_a_new_session_with_exactly_one_marker[load-fails]
FAILED …TestIdleEviction::test_an_idle_adapter_is_evicted_and_the_next_turn_respawns
FAILED …TestWedgeRetirement::test_a_wedged_turn_retires_the_adapter_and_the_next_turn_works
FAILED …TestTurnLease::test_the_lease_is_held_during_a_turn_and_released_after
FAILED …TestTurnLease::test_a_lease_held_by_a_live_process_refuses_the_turn
FAILED …TestTurnLease::test_recovery_after_a_crash_does_not_double_deliver
```

**GREEN**, CI-parity runner, per-file timeout raised because the continuity
file drives dozens of real subprocesses:

```bash
scripts/run_tests.sh tests/agent/test_claude_code_runtime.py \
                     tests/agent/test_claude_code_runtime_safety.py \
                     tests/agent/test_claude_code_session_continuity.py \
                     tests/agent/transports/ \
                     tests/hermes_cli/test_claude_code_acp_provider.py \
                     tests/hermes_cli/test_claude_code_acp_setup.py \
                     tests/providers/ --file-timeout 900
# => 22 files, 425 tests passed, 0 failed
```

Re-confirmed after the final edits, ACP files only:
`6 files, 136 tests passed, 0 failed`.

### Shared regressions

| Command | Result |
|---|---|
| `scripts/run_tests.sh tests/agent/ tests/run_agent/ --file-timeout 900` | **534 files, 5664 passed, 3 failed** |
| `scripts/run_tests.sh tests/hermes_cli/ tests/providers/ --file-timeout 900` | **575 files, 4780 passed, 2 failed** |

Every failure was checked against a **clean `HEAD` worktree** (Packet 1 only,
none of this packet's changes) rather than assumed pre-existing.

From `tests/agent/` + `tests/run_agent/`:

* `tests/agent/test_anthropic_output_field_leak.py` — 4 collection errors,
  reproduced identically on the baseline worktree. Pre-existing (Packet 1 R11).
* `tests/agent/test_credential_pool_routing.py::TestFailureAttribution::test_unmatched_key_does_not_retry_only_pool_entry`
  — reproduced identically on the baseline worktree. Pre-existing (Packet 1 R11).
* `tests/run_agent/test_70773_shared_client_fd_corruption.py::TestStaleWatchdogNeverClosesSharedClient::test_stale_stream_kill_does_not_replace_primary`
  — a streaming-watchdog timing test that fails with `stream attempt 2 was
  superseded` under heavy parallel load. It passes on the baseline worktree
  **and** in this tree when not competing with 500+ other files, and the file
  contains no `acp` / `claude_code` reference at all, so the delegated branch
  is inert in it. Recorded as contention, not as a claim that the file is
  healthy.

From `tests/hermes_cli/` + `tests/providers/`, both failures **reproduce
identically on the baseline worktree**:

* `tests/hermes_cli/test_doctor.py::test_doctor_reports_vercel_backend_diagnostics`
  (already recorded in the Packet 1 report)
* `tests/hermes_cli/test_relay_shared_metrics.py::test_concurrent_model_call_updates_are_transactional`

`tests/agent/test_compression_worker_isolation_76354.py` and
`tests/agent/test_compress_context_progress_timeout.py` also flaked under the
same load and pass on re-run here; Packet 1 recorded the first one in exactly
the same way.

### Two bugs the tests caught in the implementation itself

1. **`_drop_client()` unpublished the active turn.** It cleared `self._turn`,
   and `_connect()` calls it *inside* a turn — so from the first reconnect
   onward `request_cancel()` found `turn=None` and every user stop was silently
   dropped, leaving the turn to run to the 900 s timeout. Caught by
   `test_the_lease_is_released_after_a_cancel` hanging; fixed by making the
   turn field owned solely by `run_turn`.
2. **A fixed-sleep interrupt raced turn setup** in that same test (the failure
   mode Packet 1's closure pass already fixed once). It now waits for the
   adapter to actually hold the prompt before interrupting, like
   `_interrupt_once_prompt_lands` does in the Packet 1 suite.

### Lint / portability

```bash
ruff check agent/ tests/agent/test_claude_code_session_continuity.py   # clean
python3 scripts/check-windows-footguns.py <9 changed files>            # clean
```

## 6. Acceptance checks, mapped to tests

| Plan check | Test |
|---|---|
| 1. Turn N+1 recalls turn N | `TestOneAdapterPerSession::test_turn_two_recalls_turn_one` (+ `test_two_turns_share_one_adapter_and_one_acp_session`) |
| 2. Adapter killed → rebind via `session/load`, context intact | `TestAdapterRestart::test_killing_the_adapter_rebinds_via_session_load`, `::test_a_fresh_process_rebinds_from_the_persisted_binding` |
| 3. Load unavailable/fails → one marker, bounded recap, conversation continues | `TestReseedFallback` (7 tests, `no-load` and `load-fails` parametrised) |
| 4. Idle evicted, next turn respawns, no leaks | `TestIdleEviction::test_an_idle_adapter_is_evicted_and_the_next_turn_respawns` |
| 5. Crash mid-turn releases the lease; recovery does not double-deliver | `TestTurnLease::test_a_lease_left_by_a_dead_process_is_reclaimed`, `::test_recovery_after_a_crash_does_not_double_deliver`, `TestAdapterRestart::test_replayed_history_never_reaches_the_response_or_transcript`, `::test_a_late_replay_notification_cannot_leak_into_the_next_turn` |
| 6. No ACP-side journal — Hermes' store stays authoritative | `TestDiscardableBinding::test_acp_session_id_is_persisted_on_the_hermes_session_row`, `::test_no_second_journal_is_written` |
| Extra: one adapter per session | `TestOneAdapterPerSession::test_a_second_agent_on_the_same_session_retires_the_first_adapter`, `::test_close_reaps_the_adapter` |
| Extra: wedge retirement | `TestWedgeRetirement::test_a_wedged_turn_retires_the_adapter_and_the_next_turn_works` |
| Extra: Packet 1 security across a rebind | `TestAdapterRestart::test_permission_mode_is_renegotiated_after_a_rebind`, `TestPacketOneSecurityStillHolds` (2 tests) |
| Extra: fork isolation | `TestForkIsolation::test_a_persistence_isolated_fork_leaves_the_session_alone` |

The fake adapter keeps its session history in a store **outside** Hermes that
outlives the adapter process, so "recalls turn N after a restart" is proven by
a genuine reload, not by an in-process cache. Lease conflicts are tested
against real spawned processes (a reaped one for the stale case, a live one for
the conflict case), never a mocked pid.

## 7. Open risks

| # | Risk | Next step |
|---|---|---|
| R1 | **The background-review fork cannot work on this runtime** (§3). It is now isolated so it cannot damage the conversation, but it still burns a Claude Code turn and quota for an agent that has no Hermes tools to call. | A Packet 1 decision to revisit: either skip the fork when the resolved review runtime is `claude_code_acp`, or require `auxiliary.background_review.{provider,model}` to route it elsewhere. Recommend the former. |
| R2 | Idle TTL (30 min) and turn timeout (900 s) are module constants, not `config.yaml` keys. Deliberate — Packet 1 set the same precedent and no user has asked — but a gateway host running many Claude Code sessions may want to tune eviction. | Add `model.claude_code_acp.idle_timeout_seconds` if dogfooding asks for it (config.yaml, never an env var). |
| R3 | The turn lease is advisory: read-modify-write on the session row, no compare-and-set. Two processes starting a turn on the same session in the same instant can both win. | Only a real multi-process-per-session topology justifies a `SessionDB` CAS helper; document until then. |
| R4 | Continuity is proven against the **fake** adapter's `session/load`. Packet 0 proved real `session/load` survives an adapter restart, but the two have not been run end-to-end together on this branch. | One manual live pass before dogfood: two turns, `pkill claude-agent-acp`, third turn. |
| R5 | Hermes-side context compression and this runtime have no defined interaction. Token counts are not projected (Packet 1 R3), so the compressor should never trigger; if a session ever *is* rotated, the binding stays on the old row and the new session reseeds. | Revisit with Packet 3's usage projection, which is what would make compression reachable. |
| R6 | Windows: the reuse paths inherit Packet 1's untested-on-Windows teardown (R5 there). Nothing new is Windows-specific — eviction and retirement route through the same `close()` — but `taskkill` still has never run on a real host. | Unchanged: needs a Windows lane or a manual pass. |
| R7 | A `cwd` change mid-conversation discards the binding and reseeds. Reasonable (a loaded session is bound to its workspace) but it means a user who moves directories loses Claude-side context with only the recap to carry it. | Accepted; documented in the binding module. |
| R8 | Packet 1's R2/R4/R7b/R8/R9/R10/R11 are unchanged and still open. | As recorded in the Packet 1 report. |

## 8. Stop point

Packet 2 is complete and evidenced. **No Packet 3 work started** — no rich
event projection, no approval UX, no usage streaming, no new user-facing
commands, no second state store. Nothing was committed, pushed, or deployed.
