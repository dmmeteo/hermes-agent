# Packet 3A report — Claude Code ACP typed event projection

**Date:** 2026-08-10
**Branch:** `feat/claude-code-acp-runtime`
**Status:** Packet 3 slice **3A complete**. Permission bridging, cancellation
settling, and workspace-root negotiation (the rest of Packet 3) not started.
**Plan:** [`claude-code-acp-runtime-plan.md`](claude-code-acp-runtime-plan.md)
**Packet 2 report:** [`2026-08-10-claude-code-acp-packet2-report.md`](2026-08-10-claude-code-acp-packet2-report.md)

Nothing committed, pushed, or deployed. No user config edited — every run used
the isolated temporary `HERMES_HOME` the test harness provides.

---

## 1. What slice 3A delivers

A Claude Code turn is no longer a silent channel that only speaks at the end.

* **Tool activity reaches Hermes' existing tool-activity surfaces.**
  `tool_call` / `tool_call_update` project onto the exact four callbacks
  `agent/tool_executor.py` fires for a native tool call —
  `tool_progress_callback("tool.started"|"tool.completed", …)`,
  `tool_start_callback`, `tool_complete_callback` — so CLI bubbles, TUI/desktop
  tool cards, and messaging-platform progress all light up with no per-surface
  work. Nothing new was invented for this runtime.
* **Assistant text streams.** `agent_message_chunk` goes to
  `_fire_stream_delta`, the same sink the chat-completions path uses, so the
  CLI's own `_stream_started` / `_stream_box_opened` bookkeeping suppresses the
  duplicate response panel exactly as it does for a native turn.
  `agent_thought_chunk` goes to `_fire_reasoning_delta` (the reasoning channel),
  never to the response.
* **Usage reaches Hermes' usage accounting.** Session token counters, the
  SessionDB token queue, cost status, and the returned result dict are all
  populated. A subscription turn now reports `cost_status == "included"` instead
  of `unknown`.
* **No raw protocol payload can reach assistant text.** The reply is assembled
  from an allow-list of exactly one event kind, so a replayed
  `user_message_chunk`, a plan, a slash-command catalogue, a thought, or any
  event a future adapter version adds is ignored by default rather than
  appended.
* **Every projected field is credential-scrubbed** with
  `redact_sensitive_text(force=True)`, scrubbed *before* truncation.

Packet 1/2 posture is untouched: `session/set_mode` → `default`, every
`session/request_permission` denied, API-billing credentials stripped,
one adapter per session, lease on every path, only the new user turn sent.

## 2. Design decisions worth naming

**The assistant-text allow-list is the invariant, not a filter.** The previous
code asked "is this `agent_message_chunk`? then take the text" inline in the
session. That is the same shape, but the invariant now lives in one place with a
default-ignore branch and a test per leak class (`tc-read`, `rawOutput`,
`rawInput`, `sessionUpdate`, `toolCallId`, echoed user text, thoughts, plan
entries, command descriptions, `usage_update`). The direct-CLI failure this
runtime replaces was exactly a projection leak; making it structural is cheaper
than re-auditing every new event type.

**Tool names stay honest.** The display shows `Read`, `Bash`, `Grep` — the
adapter's own `_meta.claudeCode.toolName` — not a Hermes tool that did not run.
The codex projector maps its items onto synthetic Hermes-ish names
(`exec_command`, `apply_patch`) because codex items *have* no tool name; ACP
supplies the real one. Cost: skin `tool_emojis` lookups miss for these names and
fall back to the default glyph. Benefit: the transcript and the bubble never
claim `read_file` ran when Claude Code's `Read` did.

**Corrected 2026-08-10** (independent review H2): that reading is right for the
*display* path — `sessionUpdate: "tool_call"` always carries
`claudeCodeMetaFromToolUse` (`dist/acp-agent.js:5773-5781`). It was NOT true for
the **permission** path: adapter 0.66.0 emits `_meta` on a
`session/request_permission` only when the tool call has a parent
(`...(parentToolUseId ? { _meta: ... } : {})` at both `requestPermissionFromClient`
sites, `:3768-3772` and `:3843-3847`), so a top-level Bash/Write arrived with no
declared name and the prompt named the ACP *kind* ("execute", "edit"). The fixes
pass closes it by correlating the request's `toolCallId` with the `tool_call` the
adapter emits first (`ensureToolCallEmitted`) — see the
[fixes report](2026-08-10-claude-code-acp-packet3-fixes-report.md).

**Token counts come from the prompt reply; `usage_update` carries occupancy.**
ACP reports usage twice, and the two are not interchangeable:

| Source | Field | Meaning |
|---|---|---|
| `session/prompt` reply | `usage.{input,output,cachedRead,cachedWrite,total}Tokens` | this turn's spend — authoritative |
| `usage_update` notification | `used`, `size`, `cost` | context-window occupancy + adapter cost estimate |

A real turn emits several `usage_update`s (the Packet 0 probe saw 6 in one
turn), each restating occupancy. Summing `used` into the session counters would
have inflated accounting by roughly 20× per turn on the fake adapter's numbers
and unboundedly on a long real session. So the projector keeps a **last-wins**
occupancy snapshot, surfaced as its own `context_used_tokens` /
`context_window_tokens` result fields, and the tokens come from the reply.
`TestUsageProjection::test_context_occupancy_is_projected_separately_from_tokens`
is the guard.

**The adapter's cost figure is reported, never added.** `usage_update.cost` is
what the turn would have cost on API billing. A subscription turn costs the user
nothing extra, so it lands in the result as `adapter_reported_cost_usd` and
`session_estimated_cost_usd` stays driven by Hermes' own
`estimate_usage_cost`, which now resolves `claude-code-acp` to the
`subscription_included` billing route (one line beside the existing
`openai-codex` route — the adapter is explicitly denied the API-billing
credentials, so pricing its tokens off the Anthropic API table would invent a
charge that is never incurred).

**Hermes' context compressor is deliberately NOT driven — this closes Packet 2's
R5.** Claude Code owns its context window and compacts it itself; Hermes never
replays its transcript into the adapter. Feeding the compressor would trigger
the one cache-invalidating operation Hermes allows (rewriting past context) to
solve a problem that lives entirely on the other side of the ownership split.
`test_hermes_side_compression_is_not_driven_by_projected_usage` spies on the
real compressor instance and asserts it is never called.

**The skill-nudge counter now ticks per tool call, not per turn.** Packet 1 set
it to +1/turn with a comment calling that "one adapter-side iteration". With the
projector, the real count is known, and codex already ticks
`_iters_since_skill` by its own `tool_iterations`. A 30-tool Claude Code turn is
not one iteration of work. `max(tool_iterations, 1)` keeps the old behaviour for
a tool-free turn.

**Transcript tool rows are NOT part of this slice.** The codex runtime splices
projected `(assistant tool_calls, tool)` message pairs into `messages` so
`agent/curator.py` can see tool activity. Doing that here would change what is
persisted, what memory review reads, and what `session_search` indexes — a
separate, larger decision than "make the display work", and the plan's Packet 3
scope says *display*. The projector already counts tool iterations, so adding
the rows later is additive. Carried as R2.

**Only the projector seam was added, no permission seam.** `event_sink` on
`ClaudeAcpSession.run_turn` is the whole new interface; `_on_request` and the
blanket denial are untouched.
`TestInvariantsPreserved::test_permission_requests_are_still_denied` pins that.

## 3. A real defect found on the way: trailing updates were dropped

`ClaudeAcpClient._request_pumping` returned as soon as the reply arrived,
without draining the inbox. Notifications the agent emitted *before* the reply
can already be queued at that moment (the reader thread runs ahead of the
caller's pump), so the tail of a turn's updates was silently discarded.

The Packet 1/2 suites never caught it because the fake adapter's `normal` mode
sleeps 20 ms between its last text chunk and the reply — the tests were
unknowingly written around the race. The new `tools` mode does not sleep, and
the first GREEN attempt returned `"Reading. "` with `"Done."` and the tool's
completion event lost.

Fixed by draining every already-queued message before returning on a reply, with
the ordering argument written down: one stdio stream and one reader thread means
anything queued when the reply lands preceded it and therefore belongs to that
request. The EOF sentinel is preserved. Residual race noted as R1.

This is not a cosmetic finding: against a real adapter it drops assistant text
and tool completions from the end of a turn, which is where they cluster.

## 4. Changed files

### New (2)

| File | Purpose |
|---|---|
| `agent/transports/claude_acp_event_projector.py` | Pure ACP `session/update` → typed record projector: assistant-text allow-list, tool-activity assembly, credential scrubbing, occupancy snapshot, tool-iteration count. No agent, no callbacks, no I/O. |
| `tests/agent/test_claude_code_event_projection.py` | 44 acceptance tests, all against a real adapter subprocess over real stdio. |

### Modified (5)

| File | Change |
|---|---|
| `agent/claude_code_runtime.py` | `make_claude_acp_event_bridge()` (projected events → Hermes display callbacks, every invocation guarded); `_record_usage()` + `_queue_token_counts()`; stream-scrubber flush at turn end and on the failure path; skill counter ticks per tool call; usage merged into the result dict |
| `agent/claude_acp_session.py` | `event_sink` seam on `run_turn`; the inline text filter replaced by the projector; `AcpTurnOutcome` gains `tool_iterations`, `usage`, `context_usage` |
| `agent/transports/claude_acp.py` | `_drain_pending()` — handle notifications queued before a reply instead of dropping them (§3) |
| `agent/usage_pricing.py` | `claude-code-acp` (+ its aliases) resolves to the `subscription_included` billing route |
| `tests/agent/_fake_claude_acp_adapter.py` | New `tools` mode (full typed-event zoo, planted credentials, failed tool, update-only tool, `usage_update`, reply `usage`) and `partial-tag` mode |

Docs: this report + the plan's status line, seam table, and Packet 3 section.

## 5. RED → GREEN

**RED**, acceptance suite written, no implementation:

```bash
scripts/run_tests.sh tests/agent/test_claude_code_event_projection.py -q
# => 24 failed, 19 passed in 30.58s
```

The 19 that passed are negative guards that hold trivially when nothing is
projected at all (no leak is possible if no event is surfaced) — the same
pattern Packet 2 recorded. Representative failures, one per acceptance class:

```
FAILED …TestToolActivityProjection::test_a_tool_call_projects_a_started_event
FAILED …TestToolActivityProjection::test_started_and_completed_share_one_stable_call_id
FAILED …TestToolActivityProjection::test_a_failed_tool_is_projected_as_an_error
FAILED …TestToolActivityProjection::test_an_update_only_tool_call_still_gets_a_started_event
FAILED …TestToolActivityProjection::test_tool_iterations_advance_the_skill_counter
FAILED …TestCredentialRedaction::test_the_secret_field_is_scrubbed_rather_than_dropped
FAILED …TestStreamingProjection::test_assistant_chunks_arrive_as_deltas_in_order
FAILED …TestStreamingProjection::test_thoughts_reach_the_reasoning_surface_not_the_stream
FAILED …TestStreamingProjection::test_a_held_back_partial_tag_is_flushed_at_turn_end
FAILED …TestUsageProjection::test_per_turn_tokens_land_on_the_session_counters
FAILED …TestUsageProjection::test_a_subscription_turn_is_priced_as_included
FAILED …TestUsageProjection::test_context_occupancy_is_projected_separately_from_tokens
FAILED …TestUsageProjection::test_hermes_side_compression_is_not_driven_by_projected_usage
FAILED …TestNoRawPayloadLeaks::test_the_response_is_only_assistant_text
```

Two RED failures were the tests' own fault and were corrected rather than
implemented around: a `_Compressor` stub too thin for `turn_context`'s preflight
read (replaced by spying on the real compressor instance), and a hand-computed
`prompt_tokens` sum that contradicted `CanonicalUsage.prompt_tokens` (rewritten
as the bucket-relationship invariant, per AGENTS.md "behavior contracts over
snapshots").

**GREEN:**

```bash
scripts/run_tests.sh tests/agent/test_claude_code_runtime.py \
                     tests/agent/test_claude_code_runtime_safety.py \
                     tests/agent/test_claude_code_session_continuity.py \
                     tests/agent/test_claude_code_event_projection.py \
                     tests/agent/transports/ \
                     tests/hermes_cli/test_claude_code_acp_provider.py \
                     tests/hermes_cli/test_claude_code_acp_setup.py --file-timeout 900
# => 16 files, 419 tests passed, 0 failed in 106.3s
```

Re-confirmed after the review pass that added the already-completed-tool-call
case (§7 R-free finding: a `tool_call` arriving with `status: completed` also
owes the display an opening event):

```bash
scripts/run_tests.sh tests/agent/test_claude_code_event_projection.py -q
# => 1 file, 44 tests passed, 0 failed

scripts/run_tests.sh tests/agent/test_claude_code_runtime.py \
                     tests/agent/test_claude_code_runtime_safety.py \
                     tests/agent/test_claude_code_session_continuity.py \
                     tests/agent/transports/ \
                     tests/hermes_cli/test_claude_code_acp_provider.py \
                     tests/hermes_cli/test_claude_code_acp_setup.py --file-timeout 900
# => 15 files, 376 tests passed, 0 failed
```

### Shared regressions

| Command | Result |
|---|---|
| `scripts/run_tests.sh tests/agent/ --file-timeout 900` | **368 files, 4215 passed, 1 failed, 21 skipped** |
| `scripts/run_tests.sh tests/run_agent/ --file-timeout 900` | **169 files, 1495 passed, 1 failed, 4 skipped** |
| `scripts/run_tests.sh tests/hermes_cli/ --file-timeout 900` | **569 files, 4702 passed, 2 failed, 58 skipped** — re-run: **4703 passed, 1 failed** |
| `scripts/run_tests.sh tests/agent/test_usage_pricing.py tests/agent/test_account_usage.py tests/agent/test_billing_usage.py tests/agent/test_billing_view.py tests/agent/test_billing_links.py tests/agent/test_anthropic_billing_guidance.py tests/providers/` | **13 files, 104 passed, 0 failed** (the pricing-route change's blast radius) |
| `scripts/run_tests.sh tests/agent/ -k "usage or pricing or cost"` | **72 passed, 0 failed** |

Every failure was checked against a **clean `HEAD` git worktree** (Packets 1–2
only, none of this slice's changes) rather than assumed pre-existing:

* `tests/agent/test_credential_pool_routing.py::TestFailureAttribution::test_unmatched_key_does_not_retry_only_pool_entry`
  — fails identically on the baseline worktree in isolation. Pre-existing
  (Packet 1 R11, Packet 2 §5).
* `tests/hermes_cli/test_doctor.py::test_doctor_reports_vercel_backend_diagnostics`
  — fails identically on the baseline worktree. Pre-existing (Packet 1 and 2).
* `tests/run_agent/test_moa_loop_mode.py::test_references_run_in_parallel`
  — a wall-clock "ran in parallel" assertion. Passes in isolation in **both**
  trees (26 passed each); failed only while competing with ~170 other files.
  Recorded as contention, not as a claim the file is healthy.
* `tests/hermes_cli/test_config_read_guard.py::test_no_raw_config_yaml_reads_outside_owner_modules`
  — a repo-source `rglob` scan. Passes in isolation here (2 passed), the
  baseline worktree passes it, re-running the guard's own logic over the working
  tree reports zero offenders, and this slice's diff contains no `safe_load` at
  all. **A full re-run of `tests/hermes_cli/` passed it** (4703 passed, 1
  failed — only the pre-existing `test_doctor` case), confirming it was
  transient under load rather than a regression.
* `tests/hermes_cli/test_active_sessions.py::test_cross_process_acquire_claims_only_one_last_slot`
  and `tests/hermes_cli/test_web_server.py::TestWebServerEndpoints::test_concurrent_first_load_reads_all_succeed_on_fresh_store`
  — reported ⚠ FLAKY by the runner on the first pass (failed once, passed on
  retry) and clean on the re-run. Both are concurrency tests unrelated to this
  slice.

### Lint / portability

```bash
ruff check <7 changed files>                          # All checks passed!
python3 scripts/check-windows-footguns.py <7 files>   # ✓ No Windows footguns found
```

## 6. Acceptance mapping

Packet 3's plan checks, and what slice 3A covers:

| Plan check | Status | Test |
|---|---|---|
| 1. A tool-using turn shows structured activity, with no raw payload in text | **covered** | `TestToolActivityProjection` (9 tests), `TestNoRawPayloadLeaks` (13, one parametrised leak class each) |
| 2. A write attempt raises a real approval prompt; denial blocks the write | **not this slice** (still blanket denial) | `TestInvariantsPreserved::test_permission_requests_are_still_denied` pins the interim posture |
| 3. Approval prompts reach the user while the agent is running on a messaging platform | **not this slice** | — |
| 4. `/stop` mid-turn cancels within a bounded time; `stopReason == "cancelled"`; lease released | **pre-existing** (Packet 1/2) | `test_claude_code_runtime_safety.py::TestInterruptLatchCleared`, `…continuity.py::TestTurnLease` |
| 5. The negotiated permission mode is the one Hermes requested | **pre-existing** (Packet 1/2) | `TestPermissionSafety::test_session_mode_is_negotiated_to_default`, `TestAdapterRestart::test_permission_mode_is_renegotiated_after_a_rebind` |
| 6. Paths outside the workspace roots are refused | **pre-existing**, plus display-only proof | `tests/agent/transports/test_claude_acp.py` (`ensure_path_within_roots`), `TestInvariantsPreserved::test_projection_never_touches_a_reported_location` |

Slice-3A-specific goals from the task, mapped:

| Goal | Test |
|---|---|
| `tool_call` / `tool_call_update` → existing tool-activity events | `TestToolActivityProjection` — started/completed pairs, stable shared call id, error flag, duration, update-only call, already-completed call, non-terminal status is not an outcome, per-tool skill-counter tick |
| `agent_message_chunk` → existing streaming surface | `TestStreamingProjection::test_assistant_chunks_arrive_as_deltas_in_order`, `::test_a_held_back_partial_tag_is_flushed_at_turn_end`, `TestNoRawPayloadLeaks::test_no_protocol_payload_reaches_the_stream` |
| `usage_update` → existing usage accounting | `TestUsageProjection` (8 tests: counters, result dict, included cost, occupancy-vs-tokens, two-turn accumulation, usage-less turn, one api call, compressor untouched) |
| No raw protocol payload in assistant text | `TestNoRawPayloadLeaks` |
| Credential redaction on every projected field | `TestCredentialRedaction` (5 tests, planted `sk-ant-…` in `rawInput`/`title` and `ghp_…` in `rawOutput`) |
| Prompt-cache stability | `TestInvariantsPreserved::test_only_the_new_user_turn_is_sent` |
| Role alternation | `TestInvariantsPreserved::test_role_alternation_holds`, `TestNoRawPayloadLeaks::test_the_transcript_row_matches_the_response` |
| Workspace containment | `TestInvariantsPreserved::test_projection_never_touches_a_reported_location` |
| Zero new core tools / dependencies | No `registry.register()`, no `toolsets.py` change, no `pyproject.toml` change; the projector imports only `json`, `logging`, `time`, `dataclasses`, `typing`, and Hermes' own `agent.redact`. `tests/providers/` + provider/setup suites green. |
| Display surfaces cannot break the turn | `TestStreamingProjection::test_a_raising_display_callback_does_not_break_the_turn` |

## 7. Open risks

| # | Risk | Next step |
|---|---|---|
| R1 | **Residual ordering race in the reply drain** (§3). A notification that arrives *during* the drain is handled as part of the finishing request. For a prompt the adapter has already reported its stop reason, and for `session/load` there is no update handler at all (replayed history is dropped twice over, as Packet 2 requires), so no known path leaks. An exact fix needs the reader thread to enqueue an ordering barrier alongside the reply — a wire-layer change that touches `request()`, `drain_inbox()`, and the EOF path. | Do it with the Packet 3 permission work if a real adapter ever shows a post-reply update. |
| R2 | **Tool activity is display-only; it does not enter the transcript.** `agent/curator.py` and memory review therefore see the turn's text but not which tools ran, where codex's runtime gives them `(assistant tool_calls, tool)` pairs. | A deliberate follow-up: splice the pairs (the projector already produces everything needed) and decide the persistence/`session_search`/recap consequences in one go. |
| R3 | **Claude tool names bypass the skin emoji map.** `Read`/`Bash`/`Grep` are not Hermes tool names, so `get_tool_emoji()` falls back to the default glyph. Cosmetic, and the alternative (mapping onto Hermes names) misreports which tool ran. | Add a small ACP-name → emoji table to the skin engine if dogfooding finds it jarring. |
| R4 | **`usage_update` semantics are read from adapter 0.48.0's source**, while the pin is 0.66.0 (only 0.48.0 was resident in the npx cache on this host). The ACP SDK schema for `UsageUpdate` (`used`/`size`/`cost`) and `Usage` (`totalTokens`/`inputTokens`/`outputTokens`/`thoughtTokens`/`cachedReadTokens`/`cachedWriteTokens`) is version-stable and every field is read defensively, but the pinned adapter's per-turn accumulation has not been observed live. | One manual live probe pass before dogfood: assert reply `usage` is per-turn and `usage_update.used` is occupancy on 0.66.0. |
| R5 | **The reasoning channel now carries Claude Code's thoughts.** Gated by the existing reasoning display (`show_reasoning` / `/reasoning`), and they never reach the response, but no user has yet seen this volume of reasoning from this runtime. | Watch during dogfood; the switch is the existing reasoning config, not new surface. |
| R6 | **Cost display for subscription turns is now "included" (`$0`), not "unknown".** That is truthful for billing but hides quota consumption: a Claude subscription has rate limits, and Hermes shows no signal for how much of one a session burned. The adapter's own estimate is in the result dict as `adapter_reported_cost_usd` but nothing renders it. | Consider surfacing `context_used_tokens` / `context_window_tokens` in `/status` for delegated runtimes. |
| R7 | Packet 2's R1–R4, R6, R7 and Packet 1's open items are unchanged. R5 (compression interaction) is **closed by decision** in §2. | As recorded in those reports. |

## 8. Stop point

Slice 3A is complete and evidenced. **No permission bridging was implemented** —
`session/request_permission` is still denied for every request, the gateway's two
message guards are untouched, and no approval UX exists. No cancellation or
workspace-root work beyond what Packets 1–2 already landed. No new user-facing
command, no new core tool, no new dependency, no second state store. Nothing was
committed, pushed, or deployed, and no user config was edited.
