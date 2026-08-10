# Packet 3 review fixes — Claude Code ACP runtime

**Date:** 2026-08-10
**Branch:** `feat/claude-code-acp-runtime`
**Clean base:** `510b011dc2` ("docs: review Claude ACP Packet 3")
**Review answered:** [`2026-08-10-claude-code-acp-packet3-review.md`](2026-08-10-claude-code-acp-packet3-review.md)
**Status:** complete, **uncommitted** in the worktree. Nothing committed, pushed,
installed, deployed, or configured; no `config.yaml`, profile, or `.env` was
touched.

## Scope

All four **required** fixes (H1, H2, H3, M1), the two recommendations the task
asked for (M2, M4), and the report/plan corrections for M3, L1, L2 and the
still-deferred tool rows. Deliberately **not** implemented, per instruction:
adapter-side pre-approved-tool policy (M3's hardening), transcript-level tool
rows (M6), the Windows un-gating (M5), a Hermes-side post-cancel grace (L1), and
L3/L4/L5/L6.

Every fix was written test-first. RED evidence is the first run below, on the
clean base with only the new tests added.

---

## 1. RED → GREEN evidence

### RED — clean base `510b011dc2`, new tests only

`scripts/run_tests.sh <5 files> -j 2 --file-timeout 900`

```
=== Summary: 5 files, 99 tests passed, 22 failed (100% complete) in 139.6s (2 workers) ===

FAILED tests/agent/test_claude_code_permission_bridge.py::TestApprovalGranted::test_the_prompt_names_the_runtime_the_tool_and_the_target
FAILED …permission_bridge.py::TestUnconditionalFloor::test_a_hardline_command_is_blocked_even_under_yolo
FAILED …permission_bridge.py::TestUnconditionalFloor::test_a_declared_command_matching_a_deny_rule_is_blocked_under_yolo
FAILED …permission_bridge.py::TestUnconditionalFloor::test_a_declared_path_matching_a_deny_rule_is_blocked
FAILED …permission_bridge.py::TestUnconditionalFloor::test_a_path_that_merely_reads_like_a_hardline_command_is_not_blocked
FAILED …permission_bridge.py::TestUnconditionalFloor::test_the_bridge_applies_the_floor_to_a_projected_request
FAILED …permission_bridge.py::TestToolNamingIsHonest::test_a_top_level_request_is_named_from_the_declared_tool_call
FAILED …permission_bridge.py::TestToolNamingIsHonest::test_an_unknown_tool_call_falls_back_to_the_kind
FAILED …permission_bridge.py::TestToolNamingIsHonest::test_a_declared_name_is_not_invented_for_an_unrelated_call
FAILED …permission_bridge.py::TestGrantGrain::test_signatures_differ_past_the_display_truncation
FAILED …permission_bridge.py::TestGrantGrain::test_signatures_differ_past_the_argument_truncation
FAILED …permission_bridge.py::TestGrantGrain::test_the_signature_carries_no_declared_material
FAILED tests/agent/test_claude_code_runtime.py::TestSteerIsNotLost::test_a_steer_during_the_turn_comes_back_as_the_next_turn
FAILED tests/agent/test_claude_code_runtime.py::TestSteerIsNotLost::test_a_steer_survives_a_failed_turn
FAILED tests/agent/test_claude_code_runtime.py::TestDeterministicReaping::test_no_orphan_after_an_interrupt      # see §5 R5
FAILED tests/agent/test_claude_code_workspace_roots.py::TestPermissionLocations::test_an_out_of_root_path_past_the_displayed_slice_is_still_reported
FAILED …workspace_roots.py::TestPermissionLocations::test_shown_and_hidden_out_of_root_paths_are_both_reported
FAILED tests/agent/test_claude_code_runtime_safety.py::TestAdapterChildEnvironment::test_unrelated_provider_and_tool_secrets_do_not_reach_the_child[OPENAI_API_KEY]
FAILED …runtime_safety.py::…test_unrelated_provider_and_tool_secrets_do_not_reach_the_child[OPENROUTER_API_KEY]
FAILED …runtime_safety.py::…test_unrelated_provider_and_tool_secrets_do_not_reach_the_child[BRAVE_SEARCH_API_KEY]
FAILED …runtime_safety.py::TestAdapterChildEnvironment::test_the_subscription_token_survives_the_narrower_posture
FAILED tests/gateway/test_claude_code_acp_approval_guards.py::TestTheUserSeesTheRequest::test_the_notification_names_the_action_and_hides_credentials
```

21 of the 22 are the intended RED (the 22nd is the pre-existing timing flake in
§5 R5, which passes on its own and passed in every later run). Two of the RED rows
are **existing** tests I tightened rather than new ones — see H2 below: both
asserted a bare `"Write"` that the tool *title* satisfied on its own, so they
could not have caught the defect.

### GREEN — after the fixes, same command

```
=== Summary: 5 files, 121 tests passed, 0 failed (100% complete) in 52.8s (2 workers) ===
```

### GREEN — full focused set (8 ACP/gateway files)

`scripts/run_tests.sh tests/agent/test_claude_code_{permission_bridge,workspace_roots,runtime,runtime_safety,event_projection,cancellation,session_continuity}.py tests/gateway/test_claude_code_acp_approval_guards.py -j 2 --file-timeout 900`

```
=== Summary: 8 files, 211 tests passed, 0 failed (100% complete) in 134.2s (2 workers) ===
```

### GREEN — the surfaces the changed shared code belongs to

`tools/approval.py` and the subprocess-env helper are shared, so their own suites
ran too:

```
tests/agent/test_claude_code_event_projection.py, …cancellation.py, …session_continuity.py,
tests/tools/test_approval_deny_rules.py, …test_request_tool_approval.py, …test_approval_mode_parity.py
=== Summary: 6 files, 120 tests passed, 0 failed (100% complete) in 114.7s (2 workers) ===

tests/tools/test_approval.py, tests/tools/test_hermes_subprocess_env.py,
tests/agent/test_subprocess_env_guard.py, tests/run_agent/test_steer.py
=== Summary: 4 files, 143 tests passed, 0 failed (100% complete) in 12.5s (2 workers) ===
```

Across the three GREEN runs: **15 distinct test files, 0 failed, no FLAKY
report** (the three ACP files in the second run also ran in the first, so the
per-run counts overlap and do not sum to a unique total). Broad suites
were not run, per instruction (see §5 R1). Parallelism held at `-j 2` throughout
because of the earlier host OOM.

### Static checks

```
ruff check <11 changed source + test files>      → All checks passed!
python3 scripts/check-windows-footguns.py <5 runtime source files>
                                                 → ✓ No Windows footguns found
```

---

## 2. The fixes

### H1 — the unconditional floor now reaches the delegated runtime

`request_delegated_runtime_approval` went straight to `_run_approval_gate`, which
bypasses on yolo. The two checks `check_dangerous_command` runs *before* that
bypass — `detect_hardline_command` and `_match_user_deny_rule` — were absent, so
under `hermes --yolo` Hermes' own shell tool refused `rm -rf /` unconditionally
while the same action routed through Claude Code was auto-approved, and a user's
`approvals.deny` glob never applied to this runtime at all.

New `tools/approval.py::_delegated_runtime_floor`, called first thing in
`request_delegated_runtime_approval` via two new keyword arguments
(`declared_command`, `declared_paths`). The bridge
(`agent/claude_code_runtime.py`) passes what the runtime **declared**, not the
prompt text: a scrubbed, 120-char-truncated command is not the command the agent
asked to run.

Two judgement calls, both deliberate and both pinned by a test:

1. **The hardline detector runs on the declared command only, not on declared
   paths.** The blocklist is written against shell syntax and matches on
   substrings, so bare paths trip it by name: `detect_hardline_command(
   "/w/mkfs-notes.md")` returns `(True, "format filesystem (mkfs)")` and
   `"/tmp/kill -1.txt"` returns `(True, "kill all processes")`. Blocking an
   ordinary edit unconditionally — with no override, by design — is the "fix that
   destroys the feature" failure mode, and worse than the case it would catch.
   Paths are measured against the user's own deny globs, which are explicit and
   user-authored (`test_a_path_that_merely_reads_like_a_hardline_command_is_not_blocked`,
   `test_a_declared_path_matching_a_deny_rule_is_blocked`).
2. **The hardline block result is built without the command string.** Passing it
   would let the parser-limit arm of `_hardline_block_result` write the payload to
   `~/.hermes/cache/blocked-scripts/` and advise the model to `bash` it — Hermes-
   tool advice for an agent that lives inside the adapter and only ever learns
   "denied" from the protocol. The fail-closed behaviour on an oversized command
   is unchanged; only the file-writing recovery hint is dropped.

The declared command is taken from `rawInput.command`, falling back to `title`
**only for the `execute` kind** — the adapter copies the command verbatim into the
title for `Bash` (`dist/tools.js`, `case "Bash"`), whereas a `Write`'s title is
prose ("Write a.py") and feeding prose to a shell-syntax detector invites blocks
nobody can explain.

Positive control included: `test_yolo_still_bypasses_the_recoverable_approval_layer`
— the floor must not become a second approval prompt for ordinary work.

### H2 — the fake adapter now matches 0.66.0, and naming no longer trusts absent metadata

Verified against the pinned package (`~/.npm/_npx/3eb23f87e75affb3/…/claude-agent-acp@0.66.0`):

* both `requestPermissionFromClient` call sites emit `_meta` **only** when
  `parentToolUseId` is set (`dist/acp-agent.js:3768-3772`, `:3843-3847`), and
  `toolInfoFromToolUse` returns no `_meta` at all;
* but `requestPermissionFromClient` first `await`s `ensureToolCallEmitted`
  (`:3655`, `:3678-3699`), which sends a `tool_call` session update built by
  `toolCallNotification` → `claudeCodeMetaFromToolUse` — and *that* carries
  `toolName` unconditionally.

So the name is available, just not where 3A read it. Fixes:

* **Fake fidelity** (`tests/agent/_fake_claude_acp_adapter.py`): the `permission`
  modes now emit the referenced `tool_call` (with `_meta.claudeCode.toolName`)
  before asking, and the permission request itself carries `_meta` **only** in the
  new `permission-subagent` mode, which models a parented sub-agent call.
* **Honest naming**: `ClaudeAcpEventProjector` remembers `toolCallId → declared
  name` (only ever the adapter's own name, never a kind fallback);
  `_decide_permission` looks the id up and passes it to
  `project_permission_request(..., tool_name_hint=…)`. Precedence is the request's
  own `_meta` → the declared name from the referenced `tool_call` → the ACP kind →
  `"tool"`. Nothing is invented for a call never seen
  (`test_a_declared_name_is_not_invented_for_an_unrelated_call`).
  Ordering is safe by construction: notifications and agent-initiated requests
  share one FIFO inbox drained by the turn thread (`claude_acp.py::_dispatch`), and
  JSON-RPC over one stdio stream is ordered.
* **The two assertions that rested on the fake** now assert the qualified label
  `"Claude Code: Write"` instead of a bare `"Write"`, which the tool *title*
  satisfied on its own. That is why both went RED on the clean base once the fake
  told the truth.

On grain: the review's H2 item 2 ("the `pattern_key` is grained on the kind") does
not hold on inspection — `pattern_key` is
`delegated_runtime:{runtime}:{action}:{sha256(signature)}`, and the signature
carries kind + title + locations + args, so the grain was already per-action, not
per-kind. What was genuinely wrong was the *name shown to the user*, and M1's
collision. Both are fixed; no widening remains. This correction is stated rather
than silently skipped.

### H3 — out-of-root classification covers every declared location

`project_permission_request` classified `pairs[:_MAX_LOCATIONS]` only, so eight
in-root paths followed by `/etc/passwd` produced `outside_roots == ()` and a prompt
with no indication that the action leaves the workspace. Since 3C chose "mark,
don't refuse", that marker is the entire protection.

Now every pair is classified. Paths outside the roots that fall past the displayed
slice are reported as a count on the new field
`PermissionRequest.outside_roots_hidden`, and the description states it — either
appended to the listed ones (`… outside the workspace: /etc/shadow (+2 more
outside the workspace)`) or on its own when none of the shown paths is outside
(`… ; 1 more outside the workspace`). The pre-existing "no roots → claim nothing"
behaviour is untouched, and its test still passes.

### M1 — identity is a digest of the declared values

`signature` was `json.dumps` of the **display** view: paths scrubbed and cut to
120 chars, args cut to 2000, title cut to 120. Two siblings under a long common
prefix therefore shared one signature, and one `[a]lways` answer blanketed both —
the collision the docstrings promise cannot happen.

It is now `sha256` over `{tool, raw kind, raw title, all raw locations, raw
rawInput}`. Two consequences beyond the fix itself, both good: a redaction
collapse (two paths differing only inside a secret-shaped segment) no longer
merges two actions, and the record no longer carries the raw material — the
signature is opaque, so a caller that logs it cannot leak a `rawInput` credential
(`test_the_signature_carries_no_declared_material`). The bridge's
`sha256(signature)[:16]` rule_key is unchanged; hashing a digest is harmless.

### M2 — the adapter spawns with `inherit_credentials=False`

Proven by focused test before flipping the flag: `CLAUDE_CODE_OAUTH_TOKEN`
survives (it is explicitly `discard`ed from the Tier-2 blocklist in
`tools/environments/local.py`, precisely so an agent-spawned `claude` cannot fall
through to the shared Keychain store), while `OPENAI_API_KEY`,
`OPENROUTER_API_KEY` and `BRAVE_SEARCH_API_KEY` do not reach the child. Packet 1's
existing acceptance checks — Anthropic API-billing vars stripped, the subscription
token preserved, the parent env unmutated — all still pass.

The fake adapter records only the env vars a test names, via the new
`FAKE_ACP_WATCH_ENV`, so this did not turn into a hardcoded provider list inside
the fake.

Note: `ANTHROPIC_BASE_URL` and `CLAUDE_CODE_ACP_BASE_URL` are Tier-2 entries and
are now stripped too. Neither is consumed by this runtime (the adapter argv and
auth come from `model.claude_code_acp.*` and the subscription token); a user
pointing the adapter at a proxy through `ANTHROPIC_BASE_URL` would be relying on
inheritance that was never part of this provider's contract. Called out in §5 R3.

### M4 — a steer is delivered instead of being accepted and dropped

`steer()` returns True and parks the text in `_pending_steer`; all three drains
(the pre-API drain, the tool-result applier, `turn_finalizer`) sit behind the
delegated early return, so the CLI, gateway and TUI confirmed delivery of guidance
that was then lost.

Smallest invariant-preserving fix: `agent/claude_code_runtime.py::_steer_handoff`
drains `_pending_steer` at the end of the turn and returns it as
`result["pending_steer"]` — the *existing* contract `turn_finalizer` uses for "a
steer with no tool batch left to drain into", which `cli.py:14665`,
`gateway/run.py:26310` and `tui_gateway/server.py:10467` already deliver as the
next user turn. Both the success and the `_failure_result` paths carry it, so a
missing adapter does not eat the user's guidance either.

Rejected alternative: returning `False` from `steer()` on this runtime. The
gateway and TUI would fall back to their queue, but `cli.py:10443` prints "Steer
rejected (empty payload)." and drops the text — a false message *and* a lost
message. Role alternation is preserved either way: `pending_steer` becomes a real
next turn, never a synthetic message spliced into this one. A hard `interrupt()`
still clears the pending steer before this drain, which is the existing
"interrupt supersedes a steer" rule and is left alone.

### Report / plan corrections (M3, L1, L2, tool rows)

| Claim | Where it was | Correction |
|---|---|---|
| M3 — "negotiated roots *do* still bound those tools" | 3C R1 | True for the file tools only (`additionalDirectories` → SDK path rules, `dist/acp-agent.js:4545-4551`); it is a permission-rule input, not a sandbox, and `toolInfoFromToolUse` emits no `locations` for `Bash`, so a pre-approved `Bash(*)` writes anywhere the Hermes process can. 3B's risk 1 now also carries the `settingSources: ["user","project","local"]` evidence (`:4431`) and says the user-facing statement must include the shell caveat. |
| L1 — "a cancel the adapter never answers costs the full 900 s" | 3C R3 | The pin has `DEFAULT_FORCE_CANCEL_GRACE_MS = 30_000` (`:45`; settle sites `:1546`, `:1614`, `:1651`), so a wedged *query loop* settles in ~30 s. The residual 900 s exposure is an adapter **process** that is itself unresponsive, for which Hermes has no independent deadline. Recorded as still open. |
| L2 — "`additionalDirectories` read from 0.52.0, unverified on the pin" | 3C R4 | Now verified statically against 0.66.0: advertised as `sessionCapabilities.additionalDirectories: {}` (`:715-716`, which is why the presence-not-truthiness gate is right), honoured on create (`:763`), on load/resume (`:4275`), fed to the SDK query (`:4545-4551`); `session/set_mode` still accepts `"default"` (`:3549-3560`). Live probe still recommended before dogfood. |
| H2 — "tool names stay honest" | 3A | Amended: right for the display path, wrong for the permission path on the pin. |
| M1 — "the signature is computed over all declared paths" | 3B | Amended: all *entries*, each in its lossy display form. |
| H1 — "same allowlists … as a dangerous shell command" | 3B seam table | Amended: it skipped the pre-bypass floor until this pass. |
| H3 — the out-of-root marker | 3C §2 | Amended: the marker covered the displayed slice only. |
| Tool rows deferred | plan status | Now states the concrete cost — curator, external-memory review, background-review snapshots and `session_search` see a Claude Code turn as text with **no tool activity** — plus the two other items left open (post-cancel grace, adapter-side pre-approved policy). Not closed by silence. |

---

## 3. Changed files

### Source (5)

| File | Δ | What changed |
|---|---|---|
| `tools/approval.py` | +72 | New `_delegated_runtime_floor`; `request_delegated_runtime_approval` gains `declared_command` / `declared_paths` and applies the floor before the gate. Additive — no existing call path altered. |
| `agent/transports/claude_acp_event_projector.py` | +167/−46 | `project_permission_request`: `tool_name_hint`, all-locations root classification + `outside_roots_hidden`, digest signature over declared values, `declared_command` / `declared_paths`, new `_declared_command` helper. Projector remembers declared names (`declared_tool_name`). Module and record docstrings corrected about what is and is not scrubbed. |
| `agent/claude_acp_session.py` | +32/−4 | `_on_request` / `_decide_permission` take a `name_lookup` and pass the declared name into projection. |
| `agent/claude_code_runtime.py` | +35 | Bridge passes the declared command/paths to the floor; new `_steer_handoff` on both result paths. |
| `agent/transports/claude_acp.py` | +11/−5 | `inherit_credentials=False`, with the reasoning recorded at the spawn site. |

### Tests (6)

| File | Δ | What changed |
|---|---|---|
| `tests/agent/test_claude_code_permission_bridge.py` | +307/−1 | New `TestUnconditionalFloor` (6), `TestToolNamingIsHonest` (5), 4 new `TestGrantGrain` cases; one existing assertion tightened to the qualified label. |
| `tests/agent/test_claude_code_workspace_roots.py` | +57 | Two H3 cases (hidden-only, and shown + hidden). |
| `tests/agent/test_claude_code_runtime.py` | +47 | `TestSteerIsNotLost` (3). |
| `tests/agent/test_claude_code_runtime_safety.py` | +59 | 3 parametrised M2 cases + the "one secret in, everything else out" pair; new `record_env` fixture. |
| `tests/gateway/test_claude_code_acp_approval_guards.py` | +5/−1 | Qualified-label assertion. |
| `tests/agent/_fake_claude_acp_adapter.py` | +47/−9 | 0.66.0 `_meta` fidelity + preceding `tool_call`; new `permission-subagent` mode; `FAKE_ACP_WATCH_ENV`. |

### Docs (4)

`claude-code-acp-runtime-plan.md`, `…packet3a-report.md`,
`…packet3b-report.md`, `…packet3c-report.md` — the corrections table in §2. No
website/user docs were touched (see §5 R4).

New: this report.

---

## 4. Acceptance mapping

| Review item | Status | Evidence |
|---|---|---|
| **H1** (required) — hardline + deny floor on the delegated path | **Fixed** | `TestUnconditionalFloor` (6 tests): hardline under yolo, deny rule on a command under yolo, deny rule on a declared path, no false positive from a path that reads like a command, the floor through the real bridge, and yolo still bypassing the recoverable layer |
| **H2** (required) — fake matches 0.66.0's conditional `_meta`; naming honest without it | **Fixed** | `TestToolNamingIsHonest` (5, incl. an end-to-end `permission-subagent` turn); the two tightened assertions in the bridge + gateway suites; fake emits `_meta` only when parented |
| **H3** (required) — classify every declared location, report the overflow | **Fixed** | `test_an_out_of_root_path_past_the_displayed_slice_is_still_reported`, `test_shown_and_hidden_out_of_root_paths_are_both_reported` |
| **M1** (required) — hash raw declared paths/args | **Fixed** | `test_signatures_differ_past_the_display_truncation`, `…past_the_argument_truncation`, `…differing_only_inside_a_redacted_segment`, `test_the_signature_carries_no_declared_material` |
| **M2** (recommended) — `inherit_credentials=False` | **Implemented, proven first** | 3 parametrised strip cases + `test_the_subscription_token_survives_the_narrower_posture`; Packet 1's three env checks still green |
| **M4** (recommended) — steer honest | **Implemented (delivered, not refused)** | `TestSteerIsNotLost` (3), incl. the failure path |
| **M3** (recommended) — correct the roots claim, state the gap | **Documented** | 3C R1 + 3B risk 1 rewritten with adapter line evidence. Adapter-side policy deliberately **not** implemented |
| **L1** — force-cancel grace | **Documented** | 3C R3 rewritten; Hermes-side grace deliberately **not** implemented |
| **L2** — 0.66.0 wire compatibility | **Documented** | 3C R4 rewritten with the five verified sites |
| Tool-row persistence (M6 / 3A R2) | **Still deferred, now stated** | plan status paragraph names the blind consumers |
| M5 (Windows un-gating), L3, L4, L5 partial, L6 | **Not in this slice** | L5's absolute-invariant wording was corrected inside the projector docstring, because this pass added the two raw fields it needed to describe honestly |

Plan acceptance checks 2 and 6, which the review marked "Met, with H1/H2" and
"partial for prompts", are now met without qualification: the delegated gate has
the same pre-bypass floor as a dangerous shell command, the prompt names the tool
the adapter declared, and the prompt marker sees every declared path.

---

## 5. Unresolved risks

| # | Risk | Suggested action |
|---|---|---|
| R1 | **Broad suites not run**, per instruction — 474 tests across 15 files (ACP, gateway approval, approval core, subprocess env, steer). `tools/approval.py` changed additively and the projector change is reachable only from the permission path, so blast radius should be small, but full `tests/agent`, `tests/tools` and `tests/gateway` runs are outstanding before merge. | Run them in the pre-merge pass. |
| R2 | **`PermissionRequest` now carries two raw fields.** Documented as policy-only and never rendered, and the signature that used to hold raw-ish material is now a digest — but a future surface that prints the record wholesale would leak a declared path or command. The type is the only guard; nothing enforces it. | If this record ever reaches a new surface, print `description`. Consider a `__repr__` that omits the raw fields if that risk grows. |
| R3 | **`inherit_credentials=False` also strips `ANTHROPIC_BASE_URL` / `CLAUDE_CODE_ACP_BASE_URL`** (both Tier-2). Neither is consumed by this runtime, but a user who was pointing the adapter at a proxy through the inherited env var would silently lose it. | Mention in the provider docs if a proxy setup is ever supported; the supported knob is `model.claude_code_acp.*`. |
| R4 | **The deny-rule-on-paths semantics are new and undocumented for users.** A glob written for commands (`*passwd*`) now also blocks a delegated Write of a path it matches. That is the safer direction, but it is a behaviour a user could be surprised by. | Document `approvals.deny` as covering delegated-runtime paths when the runtime is user-facing (website docs were deliberately not touched here). |
| R5 | **One pre-existing timing flake observed**: `test_claude_code_runtime.py::TestDeterministicReaping::test_no_orphan_after_an_interrupt` failed once in the RED run at `-j 2`, passed in isolation and in all three subsequent runs of that file. Unrelated to these changes — it is an interrupt/reap race under load. | Watch it; if it recurs, loosen the wall-clock bound or make the reap check event-based. |
| R6 | **Adapter-side pre-approved tools still invisible** (M3), **no Hermes-side post-cancel grace** (L1), **no transcript tool rows** (M6), **Windows lane still skips whole ACP files** (M5). All deliberate for this slice. | M6 and M5 are cheap and self-contained; M3 needs a product decision (refuse to run, or surface once per session, when the resolved Claude settings pre-allow write/exec tools). |
| R7 | **Fake adapter, not the real one.** The `_meta` fidelity fix was derived from reading the pinned adapter's `dist/` sources, not from a live 0.66.0 session. If the real adapter ever stops emitting the `tool_call` before the permission request, naming degrades to the kind (safely, but silently). | One live probe pass before dogfood, asserting the prompt names `Bash`/`Write` against the real adapter. |

---

## 6. Stop point

Stopped here as instructed: no commit, no push, no config/deploy/profile change,
no adapter pre-approved policy, no transcript tool rows. The worktree holds 15
modified files plus this report.
