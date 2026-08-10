# Packet 3 independent review — Claude Code ACP runtime

**Date:** 2026-08-10
**Branch:** `feat/claude-code-acp-runtime`
**Range reviewed:** `03fa32c92d..33c3a73f59` (Packets 1–3), with emphasis on
`231a12cac8` (3A event projection), `b6b01c6b60` (3B permission bridge),
`33c3a73f59` (3C cancellation + workspace roots)
**Method:** read the plan, the 3A/3B/3C reports, and every changed source and
test file; verified claims against the code and against the **pinned adapter's
own source** (`@agentclientprotocol/claude-agent-acp@0.66.0`, resident in
`~/.npm/_npx/3eb23f87e75affb3/`); ran the focused ACP suites and targeted
in-process probes. No file was edited, nothing was committed, pushed, installed
or deployed.

**Verdict: ACCEPT WITH REQUIRED FIXES.**

The architecture is sound and matches the plan: the ownership split holds, the
delegated seam is placed exactly where codex's is, the projector's
assistant-text allow-list is a genuine structural invariant, cancellation
settles once with the lease released, and containment is negotiated on the wire
rather than hoped for. The test suites are unusually good — real subprocesses,
real stdio, real approval gate, real gateway guards — and they are green.

What blocks a clean accept is four defects, all in the permission/containment
path, three of which contradict claims the packet reports make about themselves:
the delegated approval gate skips the two checks that are documented to fire
*even under `--yolo`*; the approval prompt's tool naming and one gateway
assertion depend on adapter behaviour that **the pinned adapter does not
exhibit**; the out-of-root marker covers only the first eight declared paths;
and the `[a]lways` grant signature is computed over truncated display strings
rather than declared ones.

---

## Verification I ran

| Check | Result |
|---|---|
| `scripts/run_tests.sh` on the 9 ACP/gateway/transport targets, `-j 2 --file-timeout 900` | **18 files, 451 passed, 0 failed, 153.2s** — reproduces the 3C report's consolidated run exactly |
| `ruff check` on the 8 changed source files | All checks passed |
| `python3 scripts/check-windows-footguns.py` on the 5 runtime source files | No footguns found |
| Source-reading tests (AGENTS.md hard ban) in the new suites | None found |
| Adapter 0.66.0 `additionalDirectories` support | **Confirmed** (see L2) |
| Adapter 0.66.0 `_meta.claudeCode.toolName` on permission requests | **Refuted** (see H2) |
| Post-cancel 900 s hang (3C R3) | **Overstated** (see L1) |
| Adapter-side pre-approved tools (3B R1 / 3C R1) | **Confirmed, and worse than stated** (see M3) |

Probes were pure in-process calls into `project_permission_request` and
`tools.approval`; nothing touched the network, the user's config, or `~/.hermes`.

---

## Blocker

None. Nothing in this packet can corrupt state, leak a credential through a
projected field, orphan a process, or double-deliver a turn, and the
fail-closed default holds on every path I could reach.

---

## High

### H1 — The delegated approval gate skips the hardline floor **and** the user's `approvals.deny` rules

`request_delegated_runtime_approval` (`tools/approval.py:3573-3641`) goes
straight to `_run_approval_gate`. The two checks that `check_dangerous_command`
runs *before* the yolo bypass — `detect_hardline_command`
(`tools/approval.py:3442-3445`) and `_match_user_deny_rule`
(`tools/approval.py:3450-3454`) — are not in the delegated path at all.

Both are documented as unconditional. `_user_deny_block_result`
(`tools/approval.py:573-586`) tells the model the command "cannot be executed
via the agent — **not even with --yolo, /yolo, or approvals.mode=off**". That
promise is silently void for every Bash/Write the Claude Code adapter asks
about.

Probe (in-process, `_YOLO_MODE_FROZEN = True`, i.e. `hermes --yolo`):

```
native terminal tool, hardline:    False   # "Hardline block: recursive delete of root filesystem"
delegated runtime, same action:    True    # request_delegated_runtime_approval("Claude Code", "Bash", "rm -rf /")
```

So under `--yolo` — a mode users do run for coding sessions — Hermes' own shell
tool refuses `rm -rf /` unconditionally while the same action routed through
Claude Code is auto-approved. Outside yolo, a user's `approvals.deny` glob
simply never applies to this runtime.

This also contradicts the 3B report's summary of the entry point ("same
allowlists, timeout, cron policy, and fail-closed rule as a dangerous shell
command") and `tools/approval.py:3585-3589`'s own "one decision core … one
fail-closed policy".

The fix is tractable because the command text is available: for `Bash` the
adapter sets `toolCall.title` to `input.command` verbatim
(`dist/tools.js`, `case "Bash"`), which arrives as
`PermissionRequest.title`/`description`. Run the hardline detector and the deny
matcher over the declared command (and, for file tools, consider the declared
locations) before the gate.

**Required fix.**

### H2 — The permission path's `_meta.claudeCode.toolName` assumption is false on the pinned adapter; the fake adapter hides it

`_declared_tool_name` (`agent/transports/claude_acp_event_projector.py:169-182`)
reads `toolCall._meta.claudeCode.toolName`, and the 3A report's "Tool names stay
honest" decision rests on it. On the pinned adapter that meta block is emitted
on the permission request **only when the tool call has a parent** — i.e. only
for sub-agent tool use:

```js
// dist/acp-agent.js:3768-3772 and 3843-3847 (both requestPermissionFromClient sites)
...(parentToolUseId
    ? { _meta: { claudeCode: { toolName, parentToolUseId } } }
    : {}),
```

and `toolInfoFromToolUse` (`dist/tools.js:16`) returns only
`{title, kind, content, locations}` — no `_meta`. (The **display** path is
fine: `sessionUpdate: "tool_call"` does carry `claudeCodeMetaFromToolUse`,
`dist/acp-agent.js:5773-5781`. The asymmetry is specific to permissions.)

Probe against a request shaped exactly as 0.66.0 emits it:

```
Bash : tool_name='execute'  description='rm -rf /tmp/x'
Write: tool_name='edit'     description='a.py; affects /w/a.py'
```

Consequences:

1. The approval prompt on every surface reads `Claude Code: execute` /
   `Claude Code: edit` rather than `Bash` / `Write`. The title carries the
   command so the prompt is still informative, but the "honest tool name"
   property the design claims does not exist where it matters most.
2. The allowlist `pattern_key` is grained on the *kind*, widening the `[a]lways`
   grain from per-tool to per-kind.
3. `tests/gateway/test_claude_code_acp_approval_guards.py:493`
   (`assert "Write" in text`) passes only because
   `tests/agent/_fake_claude_acp_adapter.py:434` unconditionally sets
   `_meta.claudeCode.toolName`. This is the one place the fake diverges from the
   pinned adapter in a way a test asserts on — exactly 3B R7 and 3B R4, now
   confirmed rather than hypothetical.

Fix: make the fake match 0.66.0 (emit `_meta` only for parented calls, and add a
parented case), and derive the display name from `kind` + `title` with an
explicit mapping, or read the tool name out of `rawInput` shape. Do not "fix"
this by leaving the fake generous.

**Required fix** (at minimum the fake-adapter fidelity + the assertion that
rests on it).

### H3 — The out-of-root marker only looks at the first eight declared paths

3C's central user-facing containment guarantee is that a declared path outside
the negotiated roots is *marked* rather than refused (3C §2, R2). The
classification is computed over the display slice only:

```python
# agent/transports/claude_acp_event_projector.py:311-319
shown = declared[:_MAX_LOCATIONS]          # 8
outside = tuple(
    display for raw, display in pairs[:_MAX_LOCATIONS]
    if not is_within_roots(raw, roots)
)
```

Probe — eight in-root paths followed by `/etc/passwd`, roots `["/workspace"]`:

```
outside_roots: ()   omitted: 1
description: 'Edit many; affects /workspace/f0.py, … /workspace/f7.py (+1 more)'
```

The user is asked to approve an action that writes outside their workspace and
is shown **no** indication of it. This is the identical defect class 3B called
"defect, security-relevant" and fixed for the *signature* (3B §2 item 2) while
leaving the containment view on the display slice. Since 3C deliberately chose
"mark, don't refuse", the marker is the entire protection, and it has a hole
that any bulk edit trips.

Fix: classify over **all** pairs; report out-of-root paths that fall outside the
displayed slice as a count (`… and N more outside the workspace`), the same way
`omitted_locations` is reported. No test covers ≥9 locations —
`TestPermissionLocations`
(`tests/agent/test_claude_code_workspace_roots.py:485-534`) uses one path.

**Required fix.**

---

## Medium

### M1 — The `[a]lways` signature is computed over *display* strings, not declared paths

`agent/transports/claude_acp_event_projector.py:306` names the variable
`declared` but fills it with the **display** view — scrubbed and truncated to
`_MAX_PREVIEW_CHARS` (120) by `_location_pairs`
(`…:185-214`) — and that list, plus the 2000-char-truncated `args` and the
120-char `title`, is what `json.dumps` hashes at `…:323-330`.

The 3B report states the opposite as the point of its fix: "**The signature is
computed over all declared paths, not the displayed slice.**" It is computed
over all *entries*, but each entry is lossy.

Probe — two files under a 127-char common prefix:

```
collide: True   # /workspace/aaa…/alpha.py and /workspace/aaa…/beta.py share one signature
```

So a `[a]lways` answer for one deep path silently blankets its siblings, which
is precisely the collision the docstrings at `…:243-246` and
`agent/claude_code_runtime.py:305-309` promise cannot happen. Redaction is a
second (narrower) collapse: two paths differing only inside a secret-shaped
segment redact to the same string.

Exploitability is limited because `rawInput` usually carries the real path at
2000-char resolution, so the collision needs deep paths *and* absent/identical
`rawInput`. Still: hash the raw declared values, keep the display values for
display. `TestGrantGrain::test_signatures_differ_past_the_displayed_locations`
(`tests/agent/test_claude_code_permission_bridge.py:575`) uses short paths and
does not reach this.

### M2 — The adapter inherits every provider credential, though it needs exactly one

`agent/transports/claude_acp.py:219` spawns with
`hermes_subprocess_env(inherit_credentials=True)`, then pops only
`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`. Every other Tier-2 provider key
(OpenAI, OpenRouter, Gemini, xAI, Mistral, tool secrets …) is handed to a Node
process whose `Bash` tool executes model-authored commands.

`CLAUDE_CODE_OAUTH_TOKEN` — the subscription credential this provider actually
needs — is explicitly excluded from the blocklist
(`tools/environments/local.py:324-333`), so
`hermes_subprocess_env(inherit_credentials=False)` would preserve it while
stripping everything else. The helper's own docstring
(`tools/environments/local.py:601-604`) recommends exactly that pattern for
callers needing one specific secret. This is a strictly narrower posture that is
one flag away, and Packet 1's acceptance check 8 would still pass.

### M3 — Adapter-side pre-approved tools: confirmed, and the mitigating claim about roots does not hold for `Bash`

3B R1 / 3C R1 are real, and I can now evidence them from the pinned adapter
rather than by inference:

* `dist/acp-agent.js:4431` — the adapter creates its query with
  `settingSources: ["user", "project", "local"]`, so `~/.claude/settings.json`,
  `.claude/settings.json` and `.claude/settings.local.json` are all loaded,
  `permissions.allow` included. A tool the CLI's own engine allows never reaches
  `canUseTool`, so Hermes never sees a request and cannot deny, mark, or log it.
* 3C R1 offers the mitigation that "negotiated roots *do* still bound those
  tools". For file tools that is roughly true — `additionalDirectories` feeds
  the SDK's path rules (`dist/acp-agent.js:4545-4551`). For **`Bash` it is
  not**: `toolInfoFromToolUse` emits no `locations` for Bash, and
  `additionalDirectories` is a permission-rule input, not a sandbox. A
  pre-approved `Bash(*)` writes anywhere the Hermes process can.

This does not block the packet — no packet owns adapter-side policy — but the
plan's mitigation sentence should be corrected, and this must be stated to users
before dogfood (3B already flags it as "the one risk I would not ship without
stating"). A cheap hardening exists: the adapter accepts `disallowedTools` and
per-session option overrides, so Hermes could at least refuse to run when the
resolved Claude settings pre-allow write/exec tools, or surface that fact once
per session.

### M4 — `steer()` text is accepted and then silently dropped on this runtime

`AIAgent.steer()` returns `True` (surfaces report "accepted" to the user) and
parks the text in `_pending_steer`. The only drains are inside the
chat-completions loop (`agent/conversation_loop.py:1730`), the tool-result
applier, and the turn finalizer (`agent/turn_finalizer.py:717`) — all of which
the delegated early return at `agent/conversation_loop.py:1631-1643` bypasses.
Neither `agent/claude_code_runtime.py` nor `agent/claude_acp_session.py`
mentions steer. So CLI busy-input steer mode (`cli.py:10443`, `cli.py:15612`),
gateway `/queue steer` (`gateway/run.py:9155`, `:15270`) and the TUI
(`tui_gateway/server.py:7639`) all confirm delivery of guidance that is never
delivered.

Parity note: codex has the same gap for `steer()` but covers the common case
with a native `redirect()` branch (`run_agent.py:3362-3377`); Claude Code has
neither, and `redirect()` correctly falls through to "queue as next turn"
because `_model_request_active` is never set on this path. The plan lists this
as an open decision ("Steer hook … Decide: queue vs. `promptQueueing`
capability") — it is still undecided, and today's behaviour loses user input
rather than queueing it.

### M5 — Zero Windows coverage for a runtime that ships Windows-specific teardown

All five ACP suites plus the gateway guard suite are gated whole-file:

```python
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="…POSIX-only…")
```

(`tests/agent/test_claude_code_cancellation.py:47`,
`…permission_bridge.py:47`, `…runtime_safety.py:31`,
`…workspace_roots.py:49`, `tests/gateway/test_claude_code_acp_approval_guards.py:47`.)

Each reason correctly identifies only the *reaping/process-group* assertions as
POSIX-only, but the gate takes the whole file with it — including the pure
tests (`TestGrantGrain`, `TestPromptIsBounded`, `TestPermissionLocations`'s unit
cases, root validation) that would pass anywhere. Meanwhile
`agent/transports/claude_acp.py:106-158` ships a Windows-only
`taskkill /T /F` ordering argument and a `.cmd`-shim rationale that no test on
any host exercises. AGENTS.md §"Don't fake the host OS" asks for the
host-specific arm to be split out, not the file to be excluded. Split the pure
cases out (or move the process assertions behind `@pytest.mark.linux_only` at
test level) so the Windows lane runs something.

### M6 — Transcript-level tool rows (3A R2) remain open, and the plan's status line is the only place that says so

`agent/claude_code_runtime.py` never splices `(assistant tool_calls, tool)`
pairs into `messages`, where `agent/codex_runtime.py` does. Consequences:
`agent/curator.py`, memory review (`_sync_external_memory_for_turn`, called with
the tool-free `messages` at `claude_code_runtime.py:768-773`), background review
snapshots, and `session_search` all see a Claude Code turn as text with no tool
activity. Packet 3's scope bullet says *display*, so this is not an acceptance
failure — but it is a real parity gap with the sibling delegated runtime, and it
is the one item the plan itself lists as outstanding for Packet 3. It should not
be closed by silence when Packet 3 is declared done.

---

## Low

### L1 — The "post-cancel 900 s hang" (3C R3) is overstated

3C R3 says a cancel the adapter never answers costs the full
`TURN_TIMEOUT_SECONDS = 900` (`agent/claude_acp_session.py:56, 514`). The pinned
adapter has its own force-cancel path:

```js
// dist/acp-agent.js:45
const DEFAULT_FORCE_CANCEL_GRACE_MS = 30_000;
// :585-588 "Grace period before a `session/cancel` forces a wedged prompt loop
//           to return 'cancelled'."
```

with settle sites at `:1546`, `:1614`, `:1651`. So a wedged *query loop* settles
in ~30 s. The residual exposure is narrower than stated: an adapter process that
is itself unresponsive (stdout blocked, hung on a syscall) still costs 900 s,
and Hermes has no independent post-cancel deadline. Recommend updating the risk
text and, if cheap, adding a Hermes-side grace (`cancel → wait N s → retire the
adapter, settle as cancelled`), which would also cover a dead child that never
reaches EOF.

### L2 — Adapter 0.66.0 wire compatibility for `additionalDirectories` (3C R4) checks out, statically

Verified against the pinned package rather than 0.52.0:

* advertised: `agentCapabilities.sessionCapabilities.additionalDirectories: {}`
  (`dist/acp-agent.js:715-716`) — and Hermes' presence-not-truthiness gate
  (`agent/transports/claude_acp.py:309-318`) is the right test for exactly this
  `{}` shape;
* honoured on create (`:763`), on load/resume (`:4275`), and fed to the SDK
  query (`:4545-4551`);
* `loadSession` → `getOrCreateSession` → `createSession(..., {resume: id})`
  keeps the requested session id (`createSession`, "sessionId = creationOpts.resume"),
  so `agent/claude_acp_session.py:361-367` assigning `bound` after a load is
  correct;
* `session/set_mode` still accepts `"default"` (`dist/acp-agent.js:3549-3560`),
  so the negotiation Packet 1 landed is valid on the pin.

A live probe is still worth one pass before dogfood (nothing here proves Claude
Code *behaves* differently for a widened root), but the wire-shape risk is
effectively retired.

### L3 — The failure path leaves the transcript on a `user` row and is not persisted

`_failure_result` (`agent/claude_code_runtime.py:804-826`) returns without
appending an assistant message and without `agent_persisted`. The success path
guards against exactly this and says why
(`agent/claude_code_runtime.py:655-676`: "returning nothing leaves the
transcript ending on a `user` row and the next turn appends a second consecutive
one"). On the gateway, the missing key defaults to
`self._session_db is not None` (`gateway/run.py:18479`), i.e. `skip_db=True`, so
a failed Claude Code turn is written to neither store by anyone.

Codex behaves identically (`agent/codex_runtime.py:718-733`), so this is
inherited parity, not a regression — and `_merge_consecutive_roles`
(`agent/anthropic_adapter.py:2426`) repairs the alternation if the user later
switches to an API model. Worth fixing on both runtimes rather than neither.

### L4 — Idle eviction only fires on the same session's next turn

`_evict_if_idle` is called once, at the top of `run_turn`
(`agent/claude_acp_session.py:296-304, 460`). Nothing sweeps `_SESSIONS` on a
timer, so an adapter for a conversation that goes quiet lives until the agent is
closed (`run_agent.py:4405-4415`) or the interpreter exits. Bounded in practice
by gateway agent-cache eviction; the plan's "idle eviction" wording promises a
little more than the code does.

### L5 — Assistant text is deliberately unscrubbed, while the invariant is stated absolutely

`_project_text` (`agent/transports/claude_acp_event_projector.py:452-460`)
accumulates the reply raw and says why (byte-identical response). That is parity
with every native runtime and I would not change it — but the plan's
cross-cutting invariant reads "Credential scrubbing on every projected event"
and 3A §1 reads "Every projected field is credential-scrubbed". If Claude Code
quotes a `.env` it read, that text reaches the transcript and the messaging
surface unscrubbed. Restate the invariant as "every projected *tool/permission*
field", so nobody later relies on the absolute version.

### L6 — Minor protocol/registry nits

* `deny_permission_response` (`agent/transports/claude_acp.py:180-189`) will
  select a `reject_always` option if the agent lists it before `reject_once` —
  a standing adapter-side denial Hermes never intended to install. Not reachable
  on 0.66.0 (the normal path offers only `reject_once`), but the allow side is
  carefully single-shot and the deny side should mirror it.
* `ClaudeAcpSession.key` falls back to `anon-{id(self):x}`
  (`agent/claude_acp_session.py:175`); `_SESSIONS` holds strong references, so
  ids cannot be recycled while registered, but anonymous entries accumulate for
  the process lifetime.

---

## Acceptance mapping (plan Packet 3)

| Plan check | Status | Note |
|---|---|---|
| 1. Tool-using turn shows structured activity, no raw payload in text | **Met** | `TestToolActivityProjection` + 13 parametrised leak classes; allow-list is structural (`projector.py:416-436`). Display only — see M6 |
| 2. Write attempt raises a real prompt; denial blocks it; turn completes | **Met, with H1/H2** | Real gate, real surface; but the gate is missing the hardline/deny floor (H1) and the prompt names the kind, not the tool (H2) |
| 3. Approval prompts reach the user on a messaging platform | **Met** | Both guards exercised through real `BasePlatformAdapter.handle_message` and real `GatewayRunner._handle_active_session_busy_message`; the bypass is pinned as narrow |
| 4. `/stop` cancels in bounded time, `cancelled`, lease released, next turn works | **Met** | Wire-level assertions on the adapter's own `cancel_calls`; lease released in `finally` (`claude_acp_session.py:543-547`); see L1 for the wedged-process caveat |
| 5. Negotiated permission mode is the one Hermes requested | **Met** | Asserted on `set_mode_calls == ["default"]`, re-negotiated on rebind (`claude_acp_session.py:393`); `"default"` confirmed valid on the pin |
| 6. Paths outside the workspace roots are refused | **Met for `fs/*`; partial for prompts** | `_refuse_filesystem_request` (`claude_acp_session.py:589-616`) is correct and resolved-parent based; the *prompt* marker has H3's hole |
| Safety: credential scrubbing on every projected event | **Met for tool/permission fields**; see L5 |
| Safety: permissions default to deny | **Met** | Five refuse-without-asking reasons, all tested; handler-absent path denies |
| Safety: no approval bypass introduced | **Met**, but H1 means a pre-existing bypass is *inherited* where a native tool would not have one |
| Safety: cancellation leaves no orphan / held lease | **Met** | Adapter + grandchild reaped; registry cleared; lease released on the stopped-approval path too |
| Cross-cutting: prompt cache / no history replay | **Met** | Only the new turn is sent; recap only on reseed, bounded to 8 msgs / 2000 chars |
| Cross-cutting: strict role alternation | **Met on the delivered path**; L3 on the failure path |
| Deferred: transcript tool rows (3A R2) | **Open** — M6 |

---

## Required before merge

1. **H1** — run `detect_hardline_command` and `_match_user_deny_rule` on the
   declared command inside `request_delegated_runtime_approval` (or immediately
   before it in the bridge), so the "not even with `--yolo`" floor means what it
   says for delegated runtimes.
2. **H3** — classify out-of-root over every declared location, and report the
   overflow count.
3. **M1** — hash the raw declared paths/args, not their display forms.
4. **H2** — align `tests/agent/_fake_claude_acp_adapter.py` with 0.66.0's
   conditional `_meta`, fix the assertion that depends on it, and decide how the
   prompt should name a top-level tool.

## Recommended

5. **M2** — spawn with `inherit_credentials=False`; `CLAUDE_CODE_OAUTH_TOKEN`
   survives it.
6. **M3** — correct the "roots still bound those tools" claim for `Bash`, and
   state the pre-approved-tools gap to users before dogfood.
7. **M4** — either drain `_pending_steer` into the next prompt or make
   `steer()` return `False` on this runtime so surfaces queue instead of
   claiming delivery.
8. **M5** — un-gate the platform-independent tests from the Windows skip.
9. **L1/L2** — update 3C R3 and R4 with the adapter evidence above; add a
   Hermes-side post-cancel grace if dogfooding sees a wedged adapter.
10. **M6** — schedule the transcript tool rows, or record explicitly that
    curator/memory/`session_search` are blind to Claude Code tool activity.

## Things done well, worth keeping

* The assistant-text allow-list with a default-ignore branch, and one test per
  leak class. This is the right shape for the failure mode this runtime exists
  to replace.
* Tokens from the prompt reply, occupancy from `usage_update`, last-wins, never
  summed — with a test that pins the distinction, and the compressor explicitly
  not driven.
* The trailing-update drain fix (3A §3) is a genuine wire bug caught by writing
  a fake that does *not* sleep. That is the correct instinct.
* Publishing the turn before the lease write, with the reason written down
  (`claude_acp_session.py:445-449`), and `_drop_client` deliberately not
  clearing `_turn` (`:273-281`). Both are the kind of race that only shows up in
  production.
* Cancellation re-checked *after* the human answers (`:685-690`) — the half that
  is easy to miss.
* Root checks measure the declared path, never the truncated display string
  (`_location_pairs` docstring, `:185-199`), and `resolve_within_roots` uses
  resolved-parent comparison, not prefixes.
* The test suites drive real subprocesses and the real approval gate rather than
  mocks, and the reports disclose their own intermediate failures. The three
  packet reports are accurate about what was run; where they are wrong they are
  wrong about the *adapter*, not about their own work.
