# Packet 3 fixes — independent re-review

**Date:** 2026-08-10
**Branch:** `feat/claude-code-acp-runtime`
**Range re-reviewed:** `510b011dc2..7f91ac7a50` ("fix: harden Claude ACP permissions")
**Answers:** [`2026-08-10-claude-code-acp-packet3-review.md`](2026-08-10-claude-code-acp-packet3-review.md)
(required H1, H2, H3, M1; recommended M2, M4)
**Method:** read the diff and the surrounding code without relying on the
[fixes report](2026-08-10-claude-code-acp-packet3-fixes-report.md); re-derived
every adapter claim from the pinned package
(`@agentclientprotocol/claude-agent-acp@0.66.0`, `~/.npm/_npx/3eb23f87e75affb3/`);
ran the focused suites at `-j 2` and a set of in-process probes into
`project_permission_request`, the real bridge and `tools.approval`. Nothing was
edited, committed, pushed, installed, deployed, or written outside this file and
the session scratchpad; no network, no config, no `~/.hermes`.

**Verdict: ACCEPT WITH REQUIRED FIXES.**

All four required fixes are real, correctly placed and genuinely tested, and both
recommendations landed. What holds back a clean accept is one item of the *same
class as H1* that the fix pass did not carry across — the third unconditional,
pre-yolo guard on Hermes' own shell path (`sudo -S` password guessing) is still
absent from the delegated floor — together with the fixes report's claim that the
delegated gate now has "the same pre-bypass floor as a dangerous shell command"
and that plan acceptance check 2 is "met without qualification". Both are a
two-line code change and a sentence apart.

---

## Verification I ran

| Check | Result |
|---|---|
| `scripts/run_tests.sh` on the 5 changed suites, `-j 2 --file-timeout 900` | **5 files, 121 passed, 0 failed, 60.0s** |
| `scripts/run_tests.sh` on the shared-code suites (`test_approval_deny_rules`, `test_request_tool_approval`, `run_agent/test_steer`, `test_claude_code_event_projection`), `-j 2` | **4 files, 96 passed, 0 failed, 32.6s** |
| `ruff check` on the 5 changed source files | All checks passed |
| Adapter 0.66.0: permission request carries `rawInput` | **Confirmed** (`dist/acp-agent.js:3766`, `:3840` — `rawInput: toolInput` on both `requestPermissionFromClient` sites) |
| Adapter 0.66.0: `_meta` on the request only when parented | **Confirmed** (`:3768-3772`, `:3843-3847`) |
| Adapter 0.66.0: referenced `tool_call` is emitted *first*, with `toolName` unconditionally | **Confirmed** (`requestPermissionFromClient` awaits `ensureToolCallEmitted` at `:3655`; `toolCallNotification` → `claudeCodeMetaFromToolUse` at `:5763-5786`, `:5742-5754`) |
| Bash title is the command verbatim | **Confirmed** (`dist/tools.js:35-39`) |
| Hermes-side ordering guarantee for the name lookup | **Confirmed** — one FIFO inbox drained in arrival order by the turn thread (`agent/transports/claude_acp.py:639-721`); notifications and agent requests cannot overtake each other |
| Hardline / deny floor under `--yolo` through the real bridge | **Confirmed blocked** (probes below) |
| `sudo -S` guard under `--yolo` through the real bridge | **Not blocked** — see M1 |
| `CLAUDE_CODE_OAUTH_TOKEN` survives `inherit_credentials=False` | **Confirmed** (`tools/environments/local.py:333` `blocked.discard(...)`) |

Probe output (in-process, `_YOLO_MODE_FROZEN = True`, requests shaped exactly as
0.66.0 emits them — `rawInput` present, no `_meta`):

```
A  hardline under yolo approved?      False    ('rm -rf /' via rawInput.command)
A2 title-only, execute kind?          False
A4 non-execute kind, rawInput.command? False
A5 benign command under yolo?         True     (the floor is not a second prompt)
B  deny glob on a declared path?      False
B2 deny glob on the 9th path?         False    (past the display slice)
E  8 in-root + /etc/passwd, roots set: outside=() hidden=1
   description: '… (+1 more); 1 more outside the workspace'
F  deep siblings share a signature?   False
F  signature stable across call ids?  True
F  secret from rawInput in signature? False
G  declared_tool_name('tc-1')='Bash', unknown id → ''
```

---

## Blocker

None. Nothing in this diff can corrupt state, orphan a process, leak a
credential through a rendered field, or double-deliver a turn, and every new
failure path lands on deny (projection now runs *inside* the `try` that fences
the approval surface, so a raise while classifying roots denies rather than
propagates).

---

## Required-fix verification

### H1 — hardline + `approvals.deny` floor on the delegated path — **fixed**

`_delegated_runtime_floor` (`tools/approval.py:3573-3626`) runs as the first
statement of `request_delegated_runtime_approval` (`:3676-3680`), i.e. strictly
before `_run_approval_gate` and therefore before every bypass. It is fed raw
values — `PermissionRequest.declared_command` / `.declared_paths` — not the
scrubbed prompt text, which is the right input: a redacted command is not the
command.

The command is actually available in practice, which is the part worth
independent confirmation: the pinned adapter puts `rawInput: toolInput` on the
permission request at both call sites, so `rawInput.command` is present for
`Bash`, and the `execute`-kind title fallback covers the case where it is not
(probe A2). Restricting the hardline detector to the command while measuring
paths only against user-authored deny globs is the correct call, and the
false-positive control (`/w/mkfs-notes.md` stays approved) is pinned by a test.
The positive control — an ordinary command under `--yolo` still passes without a
prompt (probe A5) — matters as much as the block and is present.

Two details done well: the block result is built *without* the command, so the
parser-limit arm cannot write a model-authored payload to
`~/.hermes/cache/blocked-scripts/` on behalf of an agent that lives inside the
adapter; and a floor block still answers the wire (`reject_once`), so nothing
deadlocks.

### H2 — naming, and fake-adapter fidelity — **fixed**

I re-derived the adapter behaviour rather than trusting the report, and the fix
rests on a real invariant: `requestPermissionFromClient` **awaits**
`ensureToolCallEmitted` before sending the request, and that emission goes
through the same `toolCallNotification` helper as the streamed path, whose
`_meta.claudeCode.toolName` is unconditional. Hermes' side is ordered by
construction — one stdio stream, one reader thread, one inbox drained in order by
the turn thread — so the name is always known before the request is decided.

`ClaudeAcpEventProjector._declared_names` stores only the adapter's own declared
name (never a kind fallback), is keyed on the same raw `toolCallId` the session
looks up, and returns `""` for a call never seen; precedence is request `_meta` →
declared name → kind → `"tool"`. Nothing is invented (probe G).

The fake now emits the referenced `tool_call` before asking and carries `_meta`
on the request **only** in the new `permission-subagent` mode, matching
`...(parentToolUseId ? … : {})`. The two assertions that used to pass on the
fake's generosity now assert the qualified `"Claude Code: Write"`, which the tool
*title* cannot satisfy on its own — that is the right tightening.

The fix pass also rejected the review's H2 item 2 ("the `pattern_key` is grained
on the kind"). I checked: the old signature already carried title, locations and
args, so the grain was per-action, not per-kind. **The fix pass is right and my
predecessor's item 2 was wrong**; stating that instead of quietly implementing it
is the correct behaviour.

### H3 — out-of-root classification over every declared path — **fixed**

`project_permission_request` now enumerates all pairs, keeps the outside ones
whose index falls inside the displayed slice for listing, and counts the rest into
`outside_roots_hidden`, which `description` states in both arrangements (probe E,
plus the shown+hidden case). "No roots → claim nothing" is untouched, and a
relative declared path (`../../etc/passwd`) is classified outside, i.e.
conservatively. Two tests cover exactly the ≥9-location shape the previous
suites never reached.

### M1 — identity over declared values — **fixed**

`signature` is now `sha256` over `{tool, raw kind, raw title, all raw locations,
raw rawInput}`. Deep siblings under a >120-char common prefix no longer collide,
neither do two paths differing only inside a secret-shaped segment, the digest is
opaque (no `rawInput` material can leak through a caller that logs it), and it is
stable across `toolCallId`s — which is the property `[a]lways` depends on and
which no test states explicitly (probe F confirms it).

### M2 — credential narrowing — **implemented**

`hermes_subprocess_env(inherit_credentials=False)` with
`CLAUDE_CODE_OAUTH_TOKEN` surviving via the deliberate `blocked.discard` in
`tools/environments/local.py`. Tests assert both directions against a real
subprocess. See L1 for side effects the report only partly enumerates.

### M4 — steer — **implemented, and correctly ordered**

`_steer_handoff` drains `_pending_steer` into `result["pending_steer"]`, the
existing contract `turn_finalizer` uses, and all three surfaces already deliver
that key as the next *user* turn (`cli.py:14665`, `gateway/run.py:26307-26313`,
`tui_gateway/server.py:10467`). Nothing is spliced mid-loop, so role alternation
holds. The ordering against interrupts is right and easy to get wrong:
`agent.clear_interrupt()` (which drops a pending steer, by design) runs at
`claude_code_runtime.py:660` / `:849`, *before* the result dict is built, so a
`/stop` does not resurrect a steer on the next turn. Both the success and the
failure result carry the key, and a turn with no steer does not invent it.

---

## Medium

### M1 — the delegated floor is still missing the third unconditional guard (`sudo -S`)

`check_all_command_guards` — the guard the terminal tool actually runs — has
**three** checks ahead of the yolo/`mode=off` bypass, not two: the hardline
blocklist (`tools/approval.py:3901-3905`), the **sudo stdin guard**
(`:3911-3915`, commented "this must fire BEFORE the yolo check so even
yolo/smart approval/mode=off cannot bypass it"), and the deny rules
(`:3920-3924`). `_delegated_runtime_floor` implements the first and the third.

Probe, `_YOLO_MODE_FROZEN = True`, through the real bridge and a request shaped
as 0.66.0 emits it:

```
command: echo hunter2 | sudo -S rm -rf /var/log
delegated runtime approved under yolo?  True
native check_all_command_guards:        False   ("sudo password guessing via stdin (sudo -S)")
```

This is the identical asymmetry H1 was raised for, one guard narrower: under
`--yolo` Hermes' own shell refuses an LLM-authored `sudo -S` password guess while
the same command routed through Claude Code is auto-approved. It is a two-line
addition to `_delegated_runtime_floor` (the guard already takes a command string
and returns a ready block result via `_sudo_stdin_block_result`).

The documentation consequence is the reason I am marking this required rather
than recommended: the fixes report states the delegated entry point now has "the
same pre-bypass floor as a dangerous shell command" and that plan acceptance
check 2 is "met without qualification". Both are true against
`check_dangerous_command` (the function the previous review named) and false
against `check_all_command_guards`. Either close the gap or scope the claim.

**Required fix.**

### M2 — the floor's guarantee is still conditional on the adapter asking at all (residual, documented)

Unchanged and correctly documented by this pass (3B risk 1 / 3C R1 now carry the
`settingSources: ["user","project","local"]` evidence and the corrected Bash/roots
claim), but worth restating where it now bites hardest: a tool the CLI's own
engine pre-approves never reaches `canUseTool`, so it never produces a
`session/request_permission`, so the new floor never runs. `hermes --yolo` plus a
user `.claude/settings.json` containing `permissions.allow: ["Bash(*)"]` restores
exactly the H1 situation the floor was added to close, and neither
`_delegated_runtime_floor`'s docstring nor the bridge comment says so — both read
as if the floor is unconditional. One sentence in
`_delegated_runtime_floor`'s docstring ("this floor only measures actions the
runtime *asks* about; adapter-pre-approved tools bypass it entirely") would keep
the next reader from over-trusting it. The product-level fix (refuse to run, or
surface once per session, when the resolved Claude settings pre-allow write/exec
tools) remains unowned, as the fix pass says.

---

## Low

### L1 — the credential narrowing strips more enterprise auth than the report enumerates

The report calls out `ANTHROPIC_BASE_URL` and `CLAUDE_CODE_ACP_BASE_URL`. The
Tier-2 blocklist also contains `AWS_BEARER_TOKEN_BEDROCK`,
`GOOGLE_APPLICATION_CREDENTIALS` and `VERTEX_CREDENTIALS_PATH`, all of which are
how Claude Code authenticates on Bedrock (API-key mode) and Vertex. Those setups
inherited them before this commit (`AWS_ACCESS_KEY_ID` and
`CLAUDE_CODE_USE_BEDROCK` are *not* on the blocklist, so classic Bedrock keypair
auth still works; the bearer-token and Vertex variants no longer do). The posture
is right and I would keep it; the residual-risk row (R3) should list these three
so a dogfooder on Bedrock/Vertex knows why the adapter suddenly cannot
authenticate.

### L2 — the floor's command extraction is Bash-shaped, and degrades silently

`_declared_command` recognises a command only as `rawInput.command` (a `str`) or,
for the `execute` kind, the title. A tool declaring its command under another key,
or as a non-string, yields `""` and the hardline/deny checks simply do not run.
Probe: `rawInput={"command": ["rm", "-rf", "/"]}` with title `"run"` is approved
under `--yolo`. No pinned-adapter tool does this (`Bash` always sends a string),
and outside yolo the recoverable gate still prompts, so this is a robustness note
rather than a live hole — but the degradation is silent, and a one-line
`isinstance(command, (list, tuple))` join would remove the whole class.

### L3 — raw declared values are logged verbatim on a floor block, while the plan's invariant is still absolute

`_delegated_runtime_floor` logs `declared_command[:200]` and the matched path at
WARNING. That is exact parity with `check_all_command_guards` (`:3903`, `:3922`),
so it is not a new class — but the plan still states the invariant absolutely
("Credential scrubbing on every projected event", `claude-code-acp-runtime-plan.md:315`;
"No token, key, or credential value is read, logged, printed, or projected",
`:333`). The projector's module docstring was corrected honestly for the two raw
record fields; the plan's two lines were not, and they are the ones a future
reader will quote. (This is review L5, which the pass explicitly deferred; the
new raw fields make it slightly more load-bearing than before.)

### L4 — stale plan / report metadata

* `claude-code-acp-runtime-plan.md:81` still lists "Steer hook … Decide: queue vs
  `promptQueueing` capability" as an open decision, though M4 decided it (queue,
  via `pending_steer`).
* The fixes report's header says "**uncommitted** in the worktree … Nothing
  committed" and §6 repeats it, while the report ships *inside* commit
  `7f91ac7a50`. Self-contradictory as merged; one line to fix.

### L5 — small robustness / phrasing nits

* `declared_paths` is untyped and unpacked with `*(declared_paths or ())`; a
  caller passing a bare `str` would iterate characters. Harmless today (the only
  caller passes a tuple), cheap to guard.
* The signature material now `json.dumps`es the whole raw `rawInput`; previously
  it hashed the 2000-char-truncated view. A `Write` of a large file therefore
  serialises its full content once per permission request. Bounded by the payload
  the adapter already sent, so this is a note, not a cost.
* When no *shown* path is outside the roots, the description reads
  `…; 1 more outside the workspace` — "more" with nothing to be more *than*.
  Accurate, mildly odd; `1 further path outside the workspace` reads better.
* The signature now includes the recovered tool-name hint, so the same action
  hashed with and without a hint differs. Fail-safe (an extra prompt, never a
  wider grant) and unreachable on the pinned adapter, since the `tool_call` always
  precedes the request.

---

## Acceptance mapping (plan Packet 3, re-checked)

| Plan check | Status after `7f91ac7a50` |
|---|---|
| 1. Structured tool activity, no raw payload in text | **Met** (unchanged; display only — tool rows still deferred, now stated in the plan) |
| 2. Write attempt raises a real prompt; denial blocks it; turn completes | **Met**, with the M1 qualification above (hardline + deny floor present; sudo guard absent; adapter-pre-approved tools still never ask) |
| 3. Approval prompts reach the user on a messaging platform | **Met**, and the gateway assertion is now honest (`"Claude Code: Write"`) |
| 4. `/stop` cancels in bounded time, lease released | **Met** (unchanged; L1 of the prior review documented, no Hermes-side grace — deliberately) |
| 5. Negotiated permission mode | **Met** (unchanged) |
| 6. Paths outside the workspace roots | **Met** — `fs/*` refused; the prompt marker now sees every declared path |
| Safety: scrubbing on every projected *display* field | **Met**; the two raw policy fields are named, narrow, unrendered, and reachable only by the floor. See L3 for the plan wording |
| Safety: permissions default to deny | **Met**; projection now sits inside the fenced `try`, so a classification raise denies |
| Safety: no approval bypass introduced | **Met** — and one inherited bypass (H1) closed; one (sudo) still open, one (adapter pre-approval) still unowned |
| Cross-cutting: strict role alternation | **Met**, including the new steer handoff — it becomes a next user turn, never a mid-loop injection |

## Required before merge

1. **M1** — add `_check_sudo_stdin_guard` to `_delegated_runtime_floor`, or
   scope the fixes report's "same pre-bypass floor as a dangerous shell command"
   and "acceptance check 2 met without qualification" claims to
   `check_dangerous_command`.

## Recommended

2. **M2** — one sentence in `_delegated_runtime_floor`'s docstring that the floor
   only measures what the runtime *asks* about.
3. **L1** — list `AWS_BEARER_TOKEN_BEDROCK` / `GOOGLE_APPLICATION_CREDENTIALS` /
   `VERTEX_CREDENTIALS_PATH` in the credential-narrowing risk row.
4. **L2** — accept a list/tuple command, or log when a declared command is
   present but unrecognised.
5. **L3/L4** — restate the plan's absolute scrubbing invariant, close the plan's
   steer open-decision row, fix the report's "uncommitted" header.

## Things done well, worth keeping

* Every adapter claim in this pass is traceable to a line in the pinned `dist/`,
  and the one place the pass **disagreed** with the review (H2 item 2,
  `pattern_key` grain) it said so and showed why, instead of implementing a
  non-fix. That is the behaviour that makes a fixes report worth reading.
* The H2 fix rests on an ordering invariant that is real on both sides
  (`await ensureToolCallEmitted` in the adapter; one FIFO inbox in Hermes) rather
  than on a timing assumption, and it refuses to invent a name for a call it
  never saw.
* The floor's two judgement calls — no hardline matching on bare paths, no
  command in the block result — are each the *less obvious* choice and each the
  right one, with a false-positive control test for the first.
* Fake-adapter fidelity was fixed in the direction that makes tests harder, and
  the two assertions resting on the old generosity were tightened rather than
  deleted.
* The steer handoff reuses an existing contract three surfaces already honour,
  and lands on the correct side of `clear_interrupt()`.
* RED evidence is per-test and includes the two pre-existing tests that had to be
  tightened; the one flaky row is named as flaky rather than buried.
