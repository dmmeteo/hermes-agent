# Packet 3 slice 3C — cancellation settling + workspace containment

**Date:** 2026-08-10
**Branch:** `feat/claude-code-acp-runtime`
**Clean base:** `b6b01c6b60` ("feat: bridge Claude ACP permissions", Packet 3B)
**Plan:** [`claude-code-acp-runtime-plan.md`](claude-code-acp-runtime-plan.md) — Packet 3, scope bullets 3 and 4
**Status:** complete, **uncommitted** in the worktree. Nothing committed, pushed,
deployed, or configured; every run used the harness' per-test temporary
`HERMES_HOME`.

---

## 1. What this slice delivers

**Cancellation settles.** `/stop` was already reaching the adapter (Packets 1–2)
and the interrupt latch was already cleared. What was never pinned is how the
stopped turn *ends*. It now does, on every axis the brief named:

* the stop leaves as the ACP verb `session/cancel`, carrying the session id;
* the turn resolves within bounded time and is delivered **exactly once** — one
  assistant row, one SessionDB write, `agent_persisted: True` so the gateway
  cannot write a second, and a second `/stop` in the same turn cannot produce a
  second delivery;
* the turn lease is released on the cancelled path (already true, now also true
  for the new stopped-approval path);
* a permission request the user was still deciding on settles **fail closed**;
* nothing survives: no adapter, no registry entry, no descendant process.

**Containment is negotiated, not hoped for.** Packet 0's R4 established that the
adapter uses Claude Code's own tools and does not call back into `fs/*`, so
interception contains nothing. The session is now created with a `cwd` **plus
only the directories the user explicitly named** in
`model.claude_code_acp.additional_directories` — capability-gated on the agent
advertising `sessionCapabilities.additionalDirectories`, re-sent on every
`session/load` rebind, and used as the yardstick for anything the agent does ask
the client for. Every rejection narrows: a relative path, a missing directory, a
file, or a path already inside the workspace is dropped rather than forwarded.

**Two 3B follow-ups closed, both because they intersect this scope directly:**
R6 (interrupt-during-approval was believed correct but untested) and R3 (the
streaming scrubber's held-back tail rendering *after* an approval prompt). R3
was not taken on faith — see §3.

---

## 2. Design decisions worth naming

**The roots are session identity, not a per-request filter.** They go out on
`session/new`, so a live adapter cannot be re-scoped in place. `matches()`
therefore compares them alongside argv and cwd, and a changed root list retires
the adapter and reconnects — which, because the binding is keyed on `cwd`,
rebinds the same Claude-side session via `session/load` rather than costing a
reseed. `test_widening_the_roots_does_not_cost_the_claude_side_session` is the
guard; widening what Claude may touch should not make the conversation amnesiac.

**One implementation of "inside a root."** `resolve_within_roots` moved into
`agent/claude_acp_workspace.py` next to root negotiation, and
`ensure_path_within_roots` (the wire-layer name Packet 0 shipped and its tests
import) now delegates to it. The roots a request is checked against and the roots
the session was created with have to be the same notion of "root"; one
implementation is how that stays true.

**`fs/*` is refused — but the containment check runs first, and deliberately
so.** Hermes advertises `fs.readTextFile` / `fs.writeTextFile` as **false** at
`initialize`, so any `fs/*` request is unsolicited whatever path it names, and
serving one would hand the agent a capability it was told does not exist. The
refusal is still preceded by the root check, for two reasons: it makes the
refusal *say why* for an out-of-root path (rather than a generic
method-not-supported), and if this client ever does advertise a filesystem
capability, the gate is already in the path instead of being something a future
change has to remember to add. Both refusals are replies — silence here is the
classic ACP deadlock, and
`test_the_turn_still_completes_after_a_refused_request` pins that the turn
finishes.

**An out-of-root permission location is marked, not auto-denied.** This is the
one place I deliberately did *not* go further, and it is worth stating plainly
because the opposite choice is defensible. A `Write` request declaring a path
outside the negotiated roots now renders as `… ; outside the workspace: /x` in
the approval prompt, on every surface. It is not refused unasked.

**Corrected 2026-08-10** (independent review H3): as shipped in 3C the
classification ran over the *displayed* slice (`pairs[:_MAX_LOCATIONS]`), so a
bulk edit whose ninth declared path left the workspace was marked as staying
inside it — and since this slice chose "mark, don't refuse", that marker is the
entire protection. The fixes pass classifies **every** declared location and
reports the ones past the displayed slice as a count; see the
[fixes report](2026-08-10-claude-code-acp-packet3-fixes-report.md).

* Refusing unasked would reverse an explicit 3B decision (Hermes displays a
  declared location, never acts on it) and would flip roughly six green 3B
  approval tests from "approved" to "denied" — a behaviour change to a completed
  slice, not a gap in this one.
* The plan's own text puts containment in negotiation ("via session `cwd` +
  `additionalDirectories`, **not** via `fs/*` interception") and its acceptance
  check 6 is about paths being *refused* by `ensure_path_within_roots`, which is
  the agent-request path this slice now wires.
* The legitimate case is real: a user asking Claude Code to touch a file just
  outside the workspace should get a prompt, not a silent refusal they cannot
  override.

What was missing was **informed** consent — the user was shown a path with no
indication it was outside the scope they negotiated. That is what is fixed.
Carried as R2 below for whoever wants the stricter posture.

**Nothing is claimed when there is nothing to compare against.**
`project_permission_request(params)` with no `roots` marks nothing. Labelling
every path "outside the workspace" when the caller supplied no roots would train
users to ignore the label, which is worse than not having one.

**Display and identity stay separate.** The root check measures the **declared**
path, never the scrubbed-and-truncated display string — a truncated path resolves
to something the agent never asked about. `_location_pairs()` produces both views
together so they cannot drift out of alignment.

**The cancel re-check is after the answer, not instead of it.** `_decide_permission`
already refused without asking when the turn was cancelled *before* the request
arrived. The new check is the other half: the decision runs synchronously on the
turn thread while the adapter waits, so a `/stop` during the prompt necessarily
lands while that call is outstanding, and an approval resolving afterwards would
answer the stop with the very write the user stopped.

**The stream flush is a bare callable, not an agent handle.** 3B deliberately
gave the permission bridge no `agent` argument, because everything the gate needs
is ambient per-thread state. `make_claude_acp_permission_bridge(before_prompt=…)`
keeps that: it takes a zero-argument callback whose one job is releasing held
text, so the job cannot quietly grow into "the bridge knows about the agent".

---

## 3. R3 was real, and the test shows it

3B flagged the stream/prompt ordering as "cosmetic, flagged for 3C". The brief
said fix it only if demonstrably needed. It is. The RED run recorded this exact
event order against a real adapter that streams `"before the prompt <"` and then
asks permission:

```
['text:before the prompt ', 'prompt', 'text:<Write was declined.']
```

The scrubber held the trailing `<` (it might have opened `<think>`), the approval
prompt was drawn asking about text the user had not been shown, and the tail then
landed **after** the answer — glued onto the next sentence as `<Write was
declined.`. On a messaging surface that is a malformed message, not a cosmetic
nit. With the flush wired, the tail precedes the prompt.

---

## 4. RED → GREEN

**RED** — both acceptance suites written against the unmodified base:

```
scripts/run_tests.sh tests/agent/test_claude_code_cancellation.py \
                     tests/agent/test_claude_code_workspace_roots.py \
                     -j 1 -q --file-timeout 900
# => 2 files, 10 failed, 25 passed in 19.94s
#    cancellation:      4 failed, 12 passed
#    workspace roots:   6 failed, 13 passed
```

Failing, one per acceptance class:

```
FAILED …test_claude_code_cancellation.py::TestCancelDuringApproval::test_a_stop_mid_decision_denies_the_action
FAILED …test_claude_code_cancellation.py::TestCancelDuringApproval::test_a_stop_landing_while_the_handler_runs_denies_the_action
FAILED …test_claude_code_cancellation.py::TestStreamOrderingAroundAPrompt::test_held_back_text_reaches_the_user_before_the_prompt
FAILED …test_claude_code_workspace_roots.py::TestRootNegotiation::test_only_the_explicitly_allowed_directory_is_negotiated
FAILED …test_claude_code_workspace_roots.py::TestOutOfRootRequests::test_an_out_of_root_filesystem_request_is_refused
FAILED …test_claude_code_workspace_roots.py::TestPermissionLocations::test_an_out_of_root_location_is_marked_in_the_prompt
FAILED …test_claude_code_workspace_roots.py::TestPermissionLocations::test_an_in_root_location_is_not_marked
FAILED …test_claude_code_workspace_roots.py::TestPermissionLocations::test_with_no_roots_known_nothing_is_claimed_either_way
FAILED …test_claude_code_workspace_roots.py::TestContinuityPreserved::test_a_rebind_renegotiates_the_same_roots
```

The 25 that passed RED are the negative guards and the settle assertions Packets
1–2 already satisfied (lease release, single delivery, no orphan) — the same
pattern 3A and 3B recorded, and the reason they are still worth having: they are
what would catch this slice *breaking* them.

**One RED failure was the test's own fault** and was corrected rather than
implemented around: `test_the_interrupt_latch_is_cleared_so_the_next_turn_runs`
asserted `completed is True` on turn two, but from Packet 2 the **same** adapter
process serves turn two, so a `slow`-mode adapter is still holding its already-set
cancel flag and answers `cancelled` again. The assertion now pins what the test
is actually about — that the stop did not leak across the turn boundary
(`second["interrupted"] is False`, non-empty response) — which is the invariant,
not the fake's mode bookkeeping.

**GREEN** — new suites:

```
scripts/run_tests.sh tests/agent/test_claude_code_cancellation.py \
                     tests/agent/test_claude_code_workspace_roots.py \
                     -j 2 -q --file-timeout 900
# => 2 files, 35 tests passed, 0 failed in 28.7s
```

**GREEN** — every directly affected existing suite, unchanged by this slice:

```
scripts/run_tests.sh tests/agent/test_claude_code_permission_bridge.py \
                     tests/agent/test_claude_code_event_projection.py \
                     tests/agent/test_claude_code_runtime.py \
                     tests/agent/test_claude_code_runtime_safety.py \
                     tests/agent/test_claude_code_session_continuity.py \
                     tests/agent/transports/ \
                     tests/gateway/test_claude_code_acp_approval_guards.py \
                     -j 2 -q --file-timeout 900
# => 16 files, 414 tests passed, 0 failed in 127.7s
```

**Final consolidated run** (after the REFACTOR pass: dead `_locations()` removed,
one containment test and the gateway interrupt test added):

```
scripts/run_tests.sh tests/agent/test_claude_code_cancellation.py \
                     tests/agent/test_claude_code_workspace_roots.py \
                     tests/agent/test_claude_code_permission_bridge.py \
                     tests/agent/test_claude_code_event_projection.py \
                     tests/agent/test_claude_code_runtime.py \
                     tests/agent/test_claude_code_runtime_safety.py \
                     tests/agent/test_claude_code_session_continuity.py \
                     tests/agent/transports/ \
                     tests/gateway/test_claude_code_acp_approval_guards.py \
                     -j 2 -q --file-timeout 900
# => 18 files, 451 tests passed, 0 failed in 174.4s
```

**Approval blast radius** (the projector's permission path and the gate it feeds):

```
scripts/run_tests.sh tests/tools/test_approval.py \
                     tests/tools/test_approval_deny_rules.py \
                     tests/tools/test_approval_interrupt.py \
                     tests/tools/test_approval_mode_parity.py \
                     tests/tools/test_approval_plugin_hooks.py \
                     tests/acp/test_approval_isolation.py \
                     tests/acp/test_permissions.py \
                     tests/hermes_cli/test_claude_code_acp_provider.py \
                     tests/hermes_cli/test_claude_code_acp_setup.py \
                     -j 2 -q --file-timeout 900
# => 9 files, 187 tests passed, 0 failed in 20.6s
```

**Lint / portability:**

```
ruff check <9 changed and new files>                  # All checks passed!
python3 scripts/check-windows-footguns.py <5 source files>
                                                      # ✓ No Windows footguns found
```

Parallelism was capped at `-j 1` / `-j 2` throughout given the host OOM history;
`scripts/run_tests.sh` already runs one subprocess per file. Broad `tests/agent`
and `tests/hermes_cli` runs were **not** executed, per instruction — see R6.

---

## 5. Acceptance mapping

### Brief — cancellation settling

| Required outcome | Status | Evidence |
|---|---|---|
| Cancellation sends ACP `session/cancel` | ✅ | `TestCancelReachesTheWire::test_the_stop_is_sent_as_session_cancel` — asserted on the adapter's own recorded `cancel_calls`, with a session id |
| Exactly one clean terminal cancellation result, no double delivery | ✅ | `TestSingleDelivery` — one assistant row; `agent_persisted: True`; one `user`+`assistant` pair in SessionDB; a second `/stop` in the same turn adds nothing; a stop racing `end_turn` still delivers once |
| Releases the turn lease | ✅ | `TestNoRemainsAfterCancel::test_the_lease_is_released`, `TestCancelDuringApproval::test_the_lease_is_released_after_a_stopped_approval` (plus Packet 2's `test_the_lease_is_released_after_a_cancel`) |
| Settles pending permission waits fail-closed | ✅ | `TestCancelDuringApproval` (4 tests: end-to-end deny on a stop mid-decision, bounded settle, unit re-check, pre-arrival deny) + `tests/gateway/…::TestInterruptDuringApproval` for the messaging path |
| No adapter / session / orphan process left | ✅ | `TestNoRemainsAfterCancel::test_no_adapter_or_descendant_survives_the_session_teardown` (adapter **and** its grandchild), `::test_the_session_registry_does_not_retain_the_stopped_session` |

### Brief — workspace containment

| Required outcome | Status | Evidence |
|---|---|---|
| Negotiate `cwd` + only explicitly allowed `additionalDirectories` | ✅ | `TestRootNegotiation` (8 tests: cwd, none by default, exactly the allowed one, missing/relative/file dropped, cwd not repeated, capability gate) — all asserted on the params the adapter received on the wire |
| Reject / contain out-of-root filesystem requests | ✅ | `TestOutOfRootRequests` (6 tests: no fs capability advertised, out-of-root refusal naming the workspace, in-root refusal, the outside path is never created, the turn still completes, resolved-not-prefix matching) |
| Preserve existing restart / session continuity | ✅ | `TestContinuityPreserved` (rebind re-sends the roots on `session/load` and still recalls; widening the roots keeps the same ACP session id) + the whole Packet 2 suite green (`test_claude_code_session_continuity.py`, 0 failures) |

### Plan — Packet 3 acceptance checks

| Check | Status | Evidence |
|---|---|---|
| 4. `/stop` mid-turn cancels within a bounded time; `stopReason == "cancelled"`; lease released; the next turn works | ✅ **3C** | `TestCancelReachesTheWire` (3 tests) + `TestNoRemainsAfterCancel::test_the_lease_is_released` |
| 6. Paths outside the workspace roots are refused (`ensure_path_within_roots`) | ✅ **3C** | `TestOutOfRootRequests::test_an_out_of_root_filesystem_request_is_refused`, `::test_a_request_is_checked_against_the_roots_the_session_negotiated` |
| 1, 2, 3, 5 | unchanged | 1 landed in 3A; 2, 3, 5 in 3B — all still green in the consolidated run |

### 3B follow-up risks, as scoped by the brief

| 3B risk | Disposition |
|---|---|
| R6 — interrupt-during-approval untested | **Closed.** `tests/gateway/…::TestInterruptDuringApproval::test_a_stop_denies_the_pending_approval_and_unwedges_the_turn` proves the gateway wait resolves on the interrupt (< 60 s, against a 300 s approval ceiling), denies, writes nothing, and still leaves an assistant row. |
| R3 — CLI stream/prompt interleaving | **Closed, and it was not cosmetic.** See §3. |
| R1, R2, R4, R5, R7 | Untouched — out of this slice's scope. R1 (adapter-side pre-approved tools) remains the biggest standing gap in the permission guarantee. |

---

## 6. Changed files

### New (3)

| File | Lines | Purpose |
|---|---|---|
| `agent/claude_acp_workspace.py` | 157 | Root negotiation and the root check: reads `model.claude_code_acp.additional_directories`, validates each entry (absolute, existing, a directory, not already covered by the cwd), bounds the list, and owns `resolve_within_roots` / `is_within_roots`. No wire, no agent. |
| `tests/agent/test_claude_code_cancellation.py` | 16 tests | Cancellation settling, end to end against real adapter subprocesses. |
| `tests/agent/test_claude_code_workspace_roots.py` | 20 tests | Root negotiation, out-of-root containment, prompt marking, continuity. |

### Modified (6)

| File | Change |
|---|---|
| `agent/claude_acp_session.py` (+96/−5) | `additional_directories` on the session; `workspace_roots`; roots in `matches()`; `_negotiable_roots()` capability gate; roots passed to `session/new` **and** `session/load`; `_refuse_filesystem_request()`; the post-decision cancel re-check in `_decide_permission`; roots handed to the permission projection |
| `agent/claude_code_runtime.py` (+42/−9) | Roots resolved per turn and threaded into `_get_or_create_session`; `make_claude_acp_permission_bridge(before_prompt=…)`; the bridge now flushes the stream surface before prompting |
| `agent/transports/claude_acp.py` (+48/−18) | `supports_additional_directories` (presence, not truthiness); `additional_directories` on `session_new` / `session_load`; `ensure_path_within_roots` delegates to the workspace module |
| `agent/transports/claude_acp_event_projector.py` (+48/−14) | `_locations()` → `_location_pairs()` (declared + display, index-aligned); `PermissionRequest.outside_roots`; `project_permission_request(params, roots=…)`; the description marks out-of-root paths |
| `tests/agent/_fake_claude_acp_adapter.py` (+114/−2) | Records `session_new_params` / `session_load_params` / `client_capabilities` / `cancel_calls` / `client_request_replies`; advertises `sessionCapabilities.additionalDirectories`; new modes `fs-requests`, `no-additional-dirs`, `cancel-grandchild`, `permission-after-partial` |
| `tests/gateway/test_claude_code_acp_approval_guards.py` (+48) | `_BlockedTurn` publishes its agent; new `TestInterruptDuringApproval` (3B R6) |

Docs: this report + the plan's status header, seam table, and Packet 3 section.

**No config-schema change.** `model.claude_code_acp.additional_directories` is a
user config key read through the normal loader, exactly like the existing
`model.claude_code_acp.command`, which is likewise not in `DEFAULT_CONFIG`. No
`_config_version` bump, no new `HERMES_*` env var, no new dependency, no new core
tool, no new user-facing command.

---

## 7. Unresolved risks

| # | Risk | Next step |
|---|---|---|
| R1 | **Adapter-side pre-approved tools remain invisible** (unchanged from Packets 1/3B, and still the biggest gap in the permission guarantee). In Claude Code's `default` mode, tools already covered by the user's own allow-lists are approved inside the adapter and never produce a request Hermes can see — so neither the approval prompt nor the new out-of-root marker fires for them. Negotiated roots bound those tools **only for the file tools**: `additionalDirectories` feeds the SDK's path rules (`dist/acp-agent.js:4545-4551`), but it is a permission-rule input, not a sandbox, and `toolInfoFromToolUse` emits no `locations` for `Bash` — so a pre-approved `Bash(*)` writes anywhere the Hermes process can. **Corrected 2026-08-10** (independent review M3; the original sentence claimed roots bound all such tools). | Needs adapter-side policy control; no packet owns it. State it to users before dogfood — including that the containment claim does not extend to a pre-approved shell. |
| R2 | **An out-of-root permission request is marked, not refused.** A user who approves is still approving a write outside the negotiated workspace. Deliberate (§2), but a stricter posture — deny unasked, or require a second confirmation — is a coherent alternative someone may want. | Decide as a product call; it is a one-line change in `_decide_permission` plus a rewrite of ~6 3B approval tests. |
| R3 | **A cancel the adapter never answers still costs the full 900 s turn timeout.** `session/cancel` is fire-and-forget by design and the turn settles through the pending reply; a wedged adapter therefore settles only at `TURN_TIMEOUT_SECONDS`, after which the session is retired and the lease released. Pre-existing, not introduced here, and not testable in bounded time with the current harness. **Corrected 2026-08-10** (independent review L1): the pinned adapter has its own `DEFAULT_FORCE_CANCEL_GRACE_MS = 30_000` (`dist/acp-agent.js:45`, settle sites `:1546`, `:1614`, `:1651`), so a wedged *query loop* settles in ~30 s. The residual 900 s exposure is narrower than stated: an adapter **process** that is itself unresponsive (stdout blocked, hung on a syscall) never reaches those sites, and Hermes has no independent post-cancel deadline. | Still open: a Hermes-side grace (cancel → wait N s → retire the adapter, settle as cancelled) would also cover a dead child that never reaches EOF. Not implemented in the fixes pass. |
| R4 | **The `additionalDirectories` wire shape was read from adapter 0.52.0**, the version resident in this host's managed Node prefix, while the pin is 0.66.0. The field is an official ACP session-lifecycle parameter (present in the SDK's `newSession` / `loadSession` / `forkSession` / `resumeSession`), it is capability-gated, and the fallback (send nothing) is strictly narrower — but the pinned adapter's handling has not been observed live. **Corrected 2026-08-10** (independent review L2): the wire shape is now verified statically against the pinned 0.66.0 package — advertised as `agentCapabilities.sessionCapabilities.additionalDirectories: {}` (`dist/acp-agent.js:715-716`, which is why Hermes' presence-not-truthiness gate is the right test), honoured on create (`:763`), on load/resume (`:4275`), and fed to the SDK query (`:4545-4551`); `session/set_mode` still accepts `"default"` (`:3549-3560`). The wire-shape risk is retired. | One live probe pass before dogfood still worth doing — static evidence does not prove Claude Code *behaves* differently for a widened root. |
| R5 | **Root validation is a snapshot at session creation.** A directory that is deleted, or replaced by a symlink, after negotiation is not re-checked; the adapter holds whatever scope it was given until the session is rebuilt. | Acceptable for a workspace root; note it if roots ever become user-editable mid-session. |
| R6 | **Broad suites not run**, per instruction. Only the new tests plus the directly affected ACP / approval / provider suites ran (638 tests across 27 files, all green). The projector change is reachable only from the permission path and the workspace module is new, so blast radius should be small — but full `tests/agent` and `tests/hermes_cli` runs are outstanding before merge. | Run them in the pre-merge pass, alongside 3A's known pre-existing failures (`test_credential_pool_routing`, `test_doctor`). |
| R7 | **Fake adapter, not the real one** (plan testing policy). All evidence uses the fake ACP adapter over real stdio and real subprocesses. | Unchanged from every prior packet; the live probe covers version bumps. |

---

## 8. Stop point

Slice 3C is complete and evidenced. Not done, deliberately: no adapter-side
pre-approved tool policy, no config/deploy/profile dogfood, no transcript
persistence of tool rows (3A's R2, the one remaining Packet 3 item), no new core
tool, no commit, no push.
