# Packet 3 slice 3B — permission bridging (recovery + completion report)

**Date:** 2026-08-10
**Branch:** `feat/claude-code-acp-runtime`
**Clean base:** `231a12cac8` ("feat: project Claude Code ACP events", Packet 3A)
**Plan:** [`claude-code-acp-runtime-plan.md`](claude-code-acp-runtime-plan.md) — Packet 3, scope bullet 2
**Status:** complete, uncommitted in the worktree. Nothing committed, pushed,
deployed, or configured.

## Why this report exists

The Claude Code process working slice 3B was killed mid-flight by a host
OOM/Herdr containment incident. This session **recovered the partial work rather
than restarting it**: the uncommitted worktree already held the prior agent's
implementation (~474 changed lines) plus two new test files. That work was
inspected in full, found to be substantially complete and internally consistent,
and finished in place. No file was rewritten from scratch and no design decision
was reversed.

The single most useful finding of the recovery pass: **the prior agent's work was
functionally done and already green**. Both new suites passed on the first run
against the recovered tree (16/16 and 8/8) before any edit of mine. What was
missing was not the feature but two defects in the user-visible approval prompt
and the test coverage behind one of the implementation's louder security claims.

## What the prior process had already delivered

Recovered intact and kept as-is:

| Piece | Where | What it does |
|---|---|---|
| Approval entry point | `tools/approval.py::request_delegated_runtime_approval` | Routes a delegated runtime's action into the shared `_run_approval_gate` decision core — same allowlists, timeout, cron policy, and fail-closed rule as a dangerous shell command. Opts into `fail_closed_when_no_human=True`. |
| Protocol decision | `agent/claude_acp_session.py::_decide_permission` | Replaces Packet 1's blanket denial. Protocol routing only; no policy. Always answers (an unanswered agent request deadlocks the prompt). |
| Single-shot allow | `agent/transports/claude_acp.py::allow_permission_response` | Selects only an `allow_once` option, never `allow_always`. |
| Runtime seam | `agent/claude_code_runtime.py::make_claude_acp_permission_bridge` | Wires the handler into `run_turn`; grains the allowlist on a hash of the request signature. |
| Display projection | `agent/transports/claude_acp_event_projector.py::project_permission_request` | Scrubbed, bounded `PermissionRequest` record. Deliberately excludes protocol `options`. |
| Fake-adapter modes | `tests/agent/_fake_claude_acp_adapter.py` | `permission` now carries credential-shaped fields and an out-of-workspace location; new `permission-no-once` mode offers only `allow_always`. |

Two design decisions in that work are worth carrying forward because they are
load-bearing and non-obvious:

1. **Hermes never hands the adapter a standing grant.** Even an `[a]lways` answer
   selects the ACP `allow_once` option. A standing adapter-side grant would move
   policy out of Hermes' view — every later write would be self-approved and
   Hermes would never see the request again. Durable grants live in Hermes' own
   session/permanent allowlist, where the next request is still visible and still
   revocable.
2. **The decision runs synchronously on the turn thread.** That is what lets the
   existing gate resolve its per-thread and per-context state unaided — the CLI
   approval callback, the gateway session-key contextvar, and the interrupt flag
   are all thread- or context-local. It is also why the bridge factory
   deliberately takes no `agent` argument.

## What I changed to finish the slice

### 1. Approval-prompt paths were bounded by the *argument* budget (defect)

`_locations()` truncated each declared path with `_MAX_ARG_CHARS` (2000), while
every other human-facing field in that module uses `_MAX_PREVIEW_CHARS` (120).
With `_MAX_LOCATIONS = 8` that admitted ~16 KB of prompt text — sent verbatim to
Discord/Telegram, whose message limits are far below that, against the module's
own stated rule that "an approval prompt is user-visible text, and on a messaging
platform it leaves the machine." Paths are now display-bounded.

### 2. Locations past the eighth were silently dropped (defect, security-relevant)

`_locations()` sliced `raw[:_MAX_LOCATIONS]` at parse time, so a tool declaring
400 paths produced a prompt reading `affects a.py, b.py, …` with no signal that
392 more files were in scope. **A user approving that action was shown the wrong
action.** Fixed by splitting display from identity:

- `_locations()` now returns *every* declared path (display-bounded per path).
- `project_permission_request` shows the first `_MAX_LOCATIONS` and records the
  remainder in a new `PermissionRequest.omitted_locations` field.
- `description` reports the overflow as `(+N more)`.
- **The signature is computed over all declared paths, not the displayed slice.**
  This is the part that mattered: under the old code, two different bulk edits
  agreeing on their first eight paths produced an identical signature, so an
  `[a]lways` answer to one would silently blanket the other — precisely the
  collision the implementation's own docstrings promise cannot happen.

### 3. Coverage for the grant-grain claim (test gap)

The implementation asserts in two docstrings that answering "always" to one
action "cannot blanket a different one." Nothing tested it — the recovered tests
covered only grant *reuse* for the same action. Added `TestGrantGrain`
(5 tests, pure): same action shares a signature; different target, different
tool, and differing-past-the-displayed-locations all get distinct signatures; and
an end-to-end pass through the real gate proving a session grant for `/w/a.py`
still prompts for `/w/b.py`. Added `TestPromptIsBounded` (3 tests) for the
overflow report, the per-path bound, and an empty request.

## Changed files

Implementation (all pre-existing from the recovered work except where noted):

| File | Lines | Notes |
|---|---|---|
| `tools/approval.py` | +72 | Purely additive — one new function. No existing code path modified. |
| `agent/claude_acp_session.py` | +115/−26 | `_decide_permission`, `permission_handler` plumbing, `granted_permissions` counter. |
| `agent/claude_code_runtime.py` | +75 | Permission bridge + `RUNTIME_LABEL`. |
| `agent/transports/claude_acp.py` | +19 | `allow_permission_response`. |
| `agent/transports/claude_acp_event_projector.py` | +155/−9 | `PermissionRequest`, `project_permission_request`, `_declared_tool_name` extraction. **My changes: items 1 and 2 above.** |
| `docs/development/claude-code-acp-runtime-plan.md` | +26/−11 | Status/seam-table updates (prior agent). Verified accurate; left as written. |

Tests:

| File | Lines | Notes |
|---|---|---|
| `tests/agent/test_claude_code_permission_bridge.py` | 694 (new) | 508 recovered + 186 mine (`TestGrantGrain`, `TestPromptIsBounded`, docstring index). |
| `tests/gateway/test_claude_code_acp_approval_guards.py` | 513 (new) | Recovered unchanged. |
| `tests/agent/_fake_claude_acp_adapter.py` | +72/−8 | Recovered unchanged. |
| `tests/agent/test_claude_code_event_projection.py` | +13/−6 | Recovered: renames the interim deny test to reflect the real reason it still denies (no approval surface). |

**No gateway source changes were needed.** Both message guards already carry an
approval bypass (`gateway/run.py` ~line 9017 and the base adapter's
`handle_message`); the new tests prove it works for this runtime end to end
rather than adding a second mechanism.

## Commands run and results

Focused new suites, then directly affected existing approval/ACP suites only.
Parallelism deliberately capped (`-j 1` / `-j 2`) given the host OOM history;
`scripts/run_tests.sh` already runs one subprocess per file.

```
scripts/run_tests.sh tests/agent/test_claude_code_permission_bridge.py -j 1 -q
  → 16 passed (recovered tree, before my edits), 16.9s
  → 24 passed (final), 21.4s

scripts/run_tests.sh tests/gateway/test_claude_code_acp_approval_guards.py -j 1 -q
  → 8 passed, 11.3s

scripts/run_tests.sh tests/agent/test_claude_code_event_projection.py \
                     tests/agent/test_claude_code_runtime.py \
                     tests/agent/test_claude_code_runtime_safety.py \
                     tests/agent/test_claude_code_session_continuity.py \
                     tests/gateway/test_claude_code_acp_approval_guards.py -j 2 -q
  → 126 passed, 0 failed, 142.6s

scripts/run_tests.sh tests/tools/test_approval.py \
                     tests/tools/test_approval_config_readonly.py \
                     tests/tools/test_approval_deny_rules.py \
                     tests/tools/test_approval_interrupt.py \
                     tests/tools/test_approval_mode_parity.py \
                     tests/tools/test_approval_plugin_hooks.py \
                     tests/acp/test_approval_isolation.py \
                     tests/acp/test_permissions.py -j 2 -q
  → 152 passed, 0 failed, 14.8s

scripts/run_tests.sh tests/gateway/test_approve_deny_commands.py \
                     tests/gateway/test_plaintext_approval_routing.py \
                     tests/gateway/test_approval_prompt_redaction.py -j 2 -q
  → 28 passed, 0 failed, 7.3s

ruff check <all 8 changed/new files>  → All checks passed!
```

One intermediate failure, disclosed for completeness:
`test_one_absurd_path_cannot_blow_up_the_prompt` asserted `<= _MAX_PREVIEW_CHARS`
and saw 121 — `_truncate` appends a `…` marker. The assertion was corrected to
`+ 1` with a comment; no production code changed for it.

Broad `tests/agent` and `tests/hermes_cli` runs were **not** executed, per
instruction. See risks.

## Acceptance mapping

Plan Packet 3 acceptance checks in 3B's scope:

| Check | Status | Evidence |
|---|---|---|
| 2. A write attempt raises a real approval prompt; denial blocks the write and the turn still completes cleanly | ✅ | `TestApprovalGranted::test_an_approved_write_is_allowed_and_the_turn_completes`, `TestDenial::test_a_denied_write_is_blocked_and_the_turn_completes_cleanly` |
| 3. Approval prompts reach the user while the agent is running on a messaging platform | ✅ | `tests/gateway/test_claude_code_acp_approval_guards.py` — both guards, Telegram source, real blocked adapter |
| 5. A test asserts the negotiated permission mode is the one Hermes requested | ✅ | `TestInvariantsPreserved::test_permission_mode_is_still_negotiated_to_default` (`set_mode_calls == ["default"]`) |
| 1, 4, 6 | out of 3B | 1 landed in 3A; 4 (cancellation) and 6 (workspace roots) are 3C |

Task-brief requirements:

| Requirement | Status | Evidence |
|---|---|---|
| Uses existing Hermes approval flow (CLI/Discord/Telegram) | ✅ | `request_delegated_runtime_approval` delegates to `_run_approval_gate`; CLI via the real `set_approval_callback` seam, messaging via the real gateway notify path. No per-surface code added. |
| Both messaging guards bypassed **safely** | ✅ | `/approve`, `/deny`, bare `yes`/`no` all reach the blocked turn; `test_ordinary_text_is_still_queued_while_the_turn_runs` and `test_a_conversational_yes_is_not_an_approval_when_none_is_pending` pin the bypass as narrow |
| Default-deny on timeout / error / unavailable surface | ✅ | `TestDefaultDeny` (no surface, raising surface, timeout, no `allow_once` option, already-cancelled turn) + `test_a_timed_out_gateway_approval_denies` |
| No synthetic mid-loop user message | ✅ | `test_a_denial_adds_no_synthetic_user_message` — exactly one `user` role, strict alternation asserted pairwise. The plan's stop condition did **not** trigger. |
| Role alternation preserved | ✅ | same test |
| Prompt cache preserved (no history replay) | ✅ | `test_only_the_new_user_turn_is_sent` across two turns |
| Redaction preserved | ✅ | `test_the_prompt_is_credential_scrubbed`; gateway `test_the_notification_names_the_action_and_hides_credentials`; plus the double-scrub through `redact_sensitive_text` inside the gate |
| Default permission mode preserved | ✅ | check 5 above |
| Cancellation / workspace work left to 3C | ✅ | untouched; `_decide_permission` only *reads* `cancel_requested` to refuse without prompting |

## Unresolved risks

1. **Adapter-side pre-approved tools remain invisible.** Unchanged from Packet 1
   and still the biggest gap in the guarantee. In Claude Code's `default` mode,
   tools already covered by the user's own allow-lists
   (`~/.claude/settings.json`, project settings, session `alwaysAllow`) are
   approved inside the adapter and never produce a request Hermes can see. A
   dogfooder with a permissive Claude config will see writes land with no Hermes
   prompt. Closing this needs adapter-side policy control, which no packet
   currently owns. **This is the one risk I would not ship without stating to
   users.**
2. **Broad suites not run.** Per instruction, only the new tests and directly
   affected approval/ACP suites ran (330 tests across 17 files, all green). `tools/approval.py`
   changed additively, and the projector change is reachable only from the
   permission path, so blast radius should be nil — but full `tests/agent` and
   `tests/hermes_cli` runs are still outstanding before merge.
3. **CLI stream/prompt interleaving is untested.** The adapter can stream
   assistant text and then ask permission mid-turn, while the streaming scrubber
   holds a trailing partial tag. `_flush_stream_surface` runs only at turn end,
   so a held tail could render after the prompt is drawn. Cosmetic, and fixing it
   properly means giving the bridge an `agent` handle — which contradicts the
   deliberate no-`agent` design. Left alone; flagged for 3C, which touches this
   area anyway.
4. **`PermissionRequest.args` is projected but never displayed.** It feeds only
   the signature. Prompts therefore lean entirely on `title` + `locations`. Real
   Claude Code sets `title` (e.g. `"rm -rf /w/a.py"` for Bash), so prompts are
   informative in practice — but an adapter version that stopped setting `title`
   would degrade prompts to `"Write requested permission"` with no test catching
   it. Cheap hardening if 3C wants it.
5. **`granted_permissions` / `denied_permissions` counters have no consumer.**
   Diagnostic state only; `denied_permissions` was already unread before this
   slice. Harmless, but it is not the audit trail someone might assume it is —
   the durable record is the log line in the bridge.
6. **Interrupt-during-approval is believed correct but untested here.**
   `_await_gateway_decision`'s poll loop honours `is_interrupted()` and resolves
   to deny (approval.py ~line 3763), and the decision runs on the thread
   `AIAgent.interrupt()` flags, so `/stop` on a blocked approval should unwedge
   and refuse. No test in this slice drives that path; it belongs to 3C's
   cancellation work.
7. **Fake adapter, not the real one.** All evidence uses the fake ACP adapter
   over real stdio and real subprocesses (per the plan's testing policy). The
   real `@agentclientprotocol/claude-agent-acp@0.66.0` permission payload shape
   was pinned in Packet 0 but has not been re-probed against this bridge; the
   `_meta.claudeCode.toolName` / `locations` assumptions are the ones a version
   bump would break.

## Next

Slice 3C: cancellation settling (`session/cancel`, `stopReason == "cancelled"`,
lease release) and workspace-root negotiation (`cwd` + `additionalDirectories`),
plus transcript-level tool rows for curator/memory. The plan document's status
header and seam table were already updated by the recovered work and are accurate
as written.
