# Packet 3 — final required micro-fix (re-review M1) + doc corrections

**Date:** 2026-08-10
**Branch:** `feat/claude-code-acp-runtime`
**Clean base:** `d7149b76aa` ("docs: re-review Claude ACP hardening")
**Answers:** [`2026-08-10-claude-code-acp-packet3-rereview.md`](2026-08-10-claude-code-acp-packet3-rereview.md)
— the one **Required before merge** item (M1), plus the documentation
corrections it asked for (M2, L1, L4, L3) and the optional L2 robustness note.
**Status:** complete, **uncommitted** in the worktree. Nothing committed, pushed,
installed, deployed, or configured; no `config.yaml`, profile, `.env` or
`~/.hermes` touched; no network. Only the files listed in §4 were written.

## Scope

Implemented, per instruction:

* **M1 (required)** — the third unconditional, pre-bypass guard (`sudo -S`
  password guessing) now runs on the delegated-runtime floor, reusing the
  **native** guard rather than a re-derived pattern.
* **L2 (optional, tiny + tested)** — an argv-shaped declared command is joined
  instead of silently yielding `""`.
* Documentation corrections: the floor's "asks only" caveat (M2), the full
  enumeration of enterprise credential vars the narrowing strips (L1), the
  plan's steer open-decision row closed (L4), the fixes report's "uncommitted"
  status corrected (L4), and the plan's absolute credential-scrubbing invariant
  scoped to projected display/tool/permission fields (L3).

Deliberately **not** implemented, per instruction: broader adapter-side
pre-approved-tool policy, transcript tool rows, the Windows un-gating, a
Hermes-side post-cancel grace, and any config/deploy/profile change.

Test-first throughout. RED evidence is per-test in §1.

---

## 1. RED → GREEN evidence

### RED — clean base `d7149b76aa`, new tests only

`scripts/run_tests.sh tests/agent/test_claude_code_permission_bridge.py -j 1
--file-timeout 900`

```
=== Summary: 1 files, 40 tests passed, 5 failed (100% complete) in 18.7s (1 workers) ===

FAILED …::TestUnconditionalFloor::test_a_sudo_stdin_password_guess_is_blocked_even_under_yolo
FAILED …::TestUnconditionalFloor::test_a_sudo_stdin_password_guess_is_blocked_with_approvals_mode_off
FAILED …::TestUnconditionalFloor::test_the_bridge_blocks_a_sudo_stdin_password_guess
FAILED …::TestUnconditionalFloor::test_a_sudo_stdin_password_guess_denies_on_the_wire_under_yolo
FAILED …::TestUnconditionalFloor::test_a_command_declared_as_argv_is_still_measured
```

Per-test RED reason — each one is the bypass itself, not a missing symbol:

| Test | RED |
|---|---|
| floor, under `--yolo` | `assert decision["approved"] is False` → `assert True is False` — the delegated gate approved an LLM-authored `sudo -S` password guess |
| floor, `approvals.mode: off` | approved as well (see the honesty note below) |
| through the real bridge (`project_permission_request` → `make_claude_acp_permission_bridge`) | `AssertionError: the sudo stdin guard did not reach the delegated runtime: --yolo auto-approved an LLM-authored password guess that Hermes' own shell tool refuses unconditionally` — `handler(...)` returned `True` on `PermissionRequest(declared_command='echo hunter2 \| sudo -S rm -rf /var/log', …)` |
| end-to-end on the wire, real adapter subprocess | `permission_outcome` recorded by the adapter was `{'outcome': 'selected', 'optionId': 'allow'}` — the bypass observed **on the protocol**, not inferred |
| argv-shaped command | `assert request.declared_command == "rm -rf /"` → `assert 'run' == 'rm -rf /'`; the floor measured nothing and the bridge approved |

One positive control was written to pass on the clean base and must keep passing:
`test_a_configured_sudo_password_keeps_sudo_stdin_usable` (green in RED, green in
GREEN) — with `SUDO_PASSWORD` configured, `sudo -S` is how Hermes itself drives
sudo, so it must stay usable.

**Honesty note on the `mode: off` test.** Its first version failed for the wrong
reason: with no approval surface installed, a delegated request already
fails closed ("no interactive user or gateway is present"), so the assertion went
green-for-free on `approved is False` and only the message assertion failed. It
was rewritten to install an **approving** CLI surface, at which point RED is a
genuine approval (`assert True is False`) and the test also pins that the floor
never consults the surface.

### GREEN — after the fix

```
scripts/run_tests.sh tests/agent/test_claude_code_permission_bridge.py -j 1 --file-timeout 900
=== Summary: 1 files, 45 tests passed, 0 failed (100% complete) in 16.4s (1 workers) ===
```

Directly affected suites (approval floor + guards + projection), `-j 2`:

```
scripts/run_tests.sh tests/agent/test_claude_code_permission_bridge.py \
  tests/tools/test_hardline_blocklist.py tests/tools/test_approval.py \
  tests/tools/test_approval_deny_rules.py \
  tests/agent/test_claude_code_event_projection.py \
  tests/agent/test_claude_code_workspace_roots.py -j 2 --file-timeout 900
=== Summary: 6 files, 403 tests passed, 0 failed (100% complete) in 45.3s (2 workers) ===
```

No `⚠ FLAKY` section in either run. `ruff check` on all five changed source/test
files: **All checks passed**. Broad suites were **not** run, per instruction.

---

## 2. The fix (M1)

`_delegated_runtime_floor` (`tools/approval.py`) now runs the same three
pre-bypass checks, in the same order, that `check_all_command_guards` runs for
Hermes' own shell tool:

1. `detect_hardline_command` (already present)
2. **`_check_sudo_stdin_guard` (added)** → `_sudo_stdin_block_result`
3. `_match_user_deny_rule` (already present)

Three properties worth stating explicitly, because they are what the re-review
asked for rather than a lookalike:

* **The native guard is called, not re-derived.** `_check_sudo_stdin_guard`
  carries its own carve-out — it does not fire when the user has configured
  `SUDO_PASSWORD`, because that is the path where Hermes itself injects `-S`.
  Copying the regex would have blocked a supported setup unconditionally and
  un-overridably. Pinned by `test_a_configured_sudo_password_keeps_sudo_stdin_usable`.
* **Command-only, like the other two.** The guard measures `declared_command`
  only; `declared_paths` are still matched against user deny globs alone. A bare
  path cannot contain shell stdin syntax, and the existing false-positive
  control (`/w/mkfs-notes.md` stays approved) still holds.
* **A floor block still answers the wire.** The block is returned as a decision,
  so the bridge answers `session/request_permission` with the adapter's
  `reject_once` option. Proven end-to-end against a real adapter subprocess:
  the adapter records `{'outcome': 'selected', 'optionId': 'deny'}`, never
  writes, and the turn completes (`completed is True`, `error is None`,
  final response "Write was declined."). An unanswered request would have
  wedged the adapter until the turn timeout — that is why this assertion is on
  the recorded protocol outcome, not on the handler's return value alone.

The block is also *not* a second prompt: `test_yolo_still_bypasses_the_recoverable_approval_layer`
(pre-existing) still passes, and the two new floor tests assert
`surface.prompts == []`.

### Test-infrastructure addition

The fake adapter gained one mode, `permission-sudo-stdin` (+ the exported
`SUDO_STDIN_PASSWORD_GUESS` constant): a `Bash` call shaped exactly as pinned
adapter 0.66.0 shapes one — the command verbatim in `rawInput.command` **and**
copied into `title` (`dist/tools.js`) — with `kind: "execute"` and
`_meta.claudeCode.toolName: "Bash"` on the preceding `tool_call`. The existing
`Write` shape is unchanged; the hardcoded `"Write"` tool name in the two `_meta`
blocks became a variable so the two shapes share one code path.

### L2 — argv-shaped commands

`_declared_command` (`agent/transports/claude_acp_event_projector.py`) now joins
a `list`/`tuple` of `str` with spaces when `rawInput.command` is not a string.
Nothing on the pinned adapter sends this shape; the point is the failure mode —
an unrecognised shape returned `""` and the floor then silently measured nothing.
`rawInput={"command": ["rm","-rf","/"]}` with `title: "run"`, the exact probe from
the re-review's L2, now denies under `--yolo` instead of being approved.

---

## 3. Documentation corrections

| Item | Where | What changed |
|---|---|---|
| **M2** (recommended) | `_delegated_runtime_floor` docstring; the bridge comment in `claude_code_runtime.py` | Both now say the floor only measures the actions the runtime *asks* about — a tool the runtime's own settings pre-approve never produces a permission request and never reaches the floor. Neither reads as unconditional any more. |
| **M1** doc consequence | fixes report §4 | Dated correction: as of `7f91ac7a50` "the same pre-bypass floor as a dangerous shell command" and "acceptance check 2 met without qualification" held against `check_dangerous_command` but **not** against `check_all_command_guards` (three checks). Points at this fix and restates the pre-approval caveat. |
| **L1** | fixes report §5 R3 (+ the §2 M2 note) | R3 now enumerates all five vars the narrowing strips — `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_ACP_BASE_URL`, `AWS_BEARER_TOKEN_BEDROCK`, `GOOGLE_APPLICATION_CREDENTIALS`, `VERTEX_CREDENTIALS_PATH` — i.e. Bedrock **bearer-token** and Vertex auth, and names what still works: `CLAUDE_CODE_OAUTH_TOKEN`, classic Bedrock keypair auth, and the `CLAUDE_CODE_USE_*` / region selectors. Verified against the live blocklist, not from the report (see §5). |
| **L4** (steer) | plan, "Load-bearing seams" | The "Decide: queue vs `promptQueueing`" row is closed: **queue**, via `_steer_handoff` → `result["pending_steer"]`, delivered as the next *user* turn by all three surfaces; no mid-loop injection, no dependence on the adapter capability. |
| **L4** (status) | fixes report header + §6 | "complete, **uncommitted** … Nothing committed" now reads: written while uncommitted, subsequently committed as `7f91ac7a50`; still nothing pushed/installed/deployed/configured. The self-contradiction of a report shipping inside the commit it calls uncommitted is gone. |
| **L3** | plan `:315`, `:334`, `:271` | The absolute scrubbing claims are scoped to projected **display/tool/permission** fields, with the two deliberate exceptions named where a reader will quote them: assistant reply text (byte-identical response; surfaces scrub downstream) and `PermissionRequest.declared_command` / `.declared_paths` (raw, never rendered, policy-only) — including that the floor logs a truncated declared value at WARNING, exactly as `check_all_command_guards` does. |

---

## 4. Changed files

| File | Δ | What |
|---|---|---|
| `tools/approval.py` | +29/−8 | Sudo stdin guard on the floor; docstrings updated to "three checks" + the asks-only caveat |
| `agent/transports/claude_acp_event_projector.py` | +11/−0 | `_declared_command` accepts an argv list/tuple |
| `agent/claude_code_runtime.py` | +8/−5 | Bridge comment: three checks, and the pre-approval caveat |
| `tests/agent/test_claude_code_permission_bridge.py` | +158/−4 | 5 new tests + 1 positive control; class docstring corrected to three checks |
| `tests/agent/_fake_claude_acp_adapter.py` | +41/−12 | `permission-sudo-stdin` mode, `SUDO_STDIN_PASSWORD_GUESS`, tool name made per-shape |
| `docs/development/2026-08-10-claude-code-acp-packet3-fixes-report.md` | +25/−8 | Status, R3 enumeration, M1 dated correction |
| `docs/development/claude-code-acp-runtime-plan.md` | +21/−8 | Steer decision closed; scrubbing invariant scoped |
| `docs/development/2026-08-10-claude-code-acp-packet3-finalfix-report.md` | new | This report |

---

## 5. Independent probes

In-process, `_YOLO_MODE_FROZEN = True`, `approvals.deny` empty, request shaped as
0.66.0 emits it (`rawInput` present, no `_meta`), against a scratchpad
`HERMES_HOME`:

```
command:                          echo hunter2 | sudo -S rm -rf /var/log
declared_command (projected):     echo hunter2 | sudo -S rm -rf /var/log
bridge approves under yolo?       False
native check_all_command_guards:  False          ← parity, the asymmetry is closed
gate message:                     BLOCKED: sudo password guessing via stdin (sudo -S). …
benign command under yolo?        True            ← the floor is not a second prompt
argv command ['rm','-rf','/']:    'rm -rf /' → approved? False
sudo -S with SUDO_PASSWORD set:   approved? True  ← the native carve-out survives
```

Credential-narrowing enumeration, read from the live
`_HERMES_PROVIDER_ENV_BLOCKLIST` rather than from the earlier report:

```
AWS_BEARER_TOKEN_BEDROCK         blocked=True     GOOGLE_APPLICATION_CREDENTIALS  blocked=True
VERTEX_CREDENTIALS_PATH          blocked=True     ANTHROPIC_BASE_URL              blocked=True
CLAUDE_CODE_ACP_BASE_URL         blocked=True     CLAUDE_CODE_OAUTH_TOKEN         blocked=False
AWS_ACCESS_KEY_ID                blocked=False    CLAUDE_CODE_USE_BEDROCK         blocked=False
CLAUDE_CODE_USE_VERTEX           blocked=False    ANTHROPIC_VERTEX_PROJECT_ID     blocked=False
```

`AWS_BEARER_TOKEN_BEDROCK` enters the blocklist through the `auth_type="aws_sdk"`
branch of `_build_provider_env_blocklist()`; the other two are unconditional
entries. The general AWS chain is deliberately inheritable
(`_AWS_SDK_CREDENTIAL_ENV_VARS` is scoped narrow on purpose, with the reasoning
in `tools/environments/local.py`).

---

## 6. Acceptance mapping — the two rows this fix moves

| Plan check | Before (`d7149b76aa`) | After |
|---|---|---|
| 2. Write attempt raises a real prompt; denial blocks it; turn completes | Met, with the re-review's M1 qualification (hardline + deny floor present, **sudo guard absent**) | **Met.** All three `check_all_command_guards` pre-bypass checks now hold on the delegated path, proven on the wire. The remaining, separately-owned qualification is unchanged and now documented in both the code and the report: adapter-pre-approved tools never ask, so the floor never sees them. |
| Safety: no approval bypass introduced | One inherited bypass (sudo) still open | **Closed.** One inherited bypass remains **unowned** (adapter pre-approval) — a product decision, out of scope here, and now stated wherever the floor is described. |

---

## 7. Unresolved / deliberately not done

| # | Item | Note |
|---|---|---|
| R1 | **Broad suites not run**, per instruction. `tools/approval.py` changed additively inside one function; the projector change is reachable only from the permission path. Full `tests/agent` / `tests/tools` / `tests/gateway` runs are outstanding before merge. | Run in the pre-merge pass. |
| R2 | **Adapter-pre-approved tools still bypass the floor entirely.** `hermes --yolo` plus a user `.claude/settings.json` with `permissions.allow: ["Bash(*)"]` restores the original asymmetry. Now documented in three places instead of zero, but not fixed. | Needs the product decision the re-review named: refuse to run, or surface once per session, when the resolved Claude settings pre-allow write/exec tools. |
| R3 | **`declared_paths` is still untyped** (re-review L5): a caller passing a bare `str` would iterate characters. Harmless today — the only caller passes a tuple — and out of the requested scope. | One `isinstance` guard, whenever that file is next touched. |
| R4 | The argv join is unquoted (`" ".join`). For a *detector* this is the conservative direction (it can only match more), but a path containing spaces produces text no shell would have parsed that way. Unreachable on the pinned adapter, which always sends a string. | Note only. |
| R5 | Other still-open re-review items untouched by instruction: L5's remaining phrasing nits ("1 further path outside the workspace"), the signature's full-`rawInput` serialisation cost note, transcript tool rows (M6), Windows un-gating (M5), Hermes-side post-cancel grace. | All deliberate. |

---

## 8. Stop point

Stopped here as instructed: no commit, no push, no deploy, no install, no
config/profile/`.env` change, no broader adapter policy, no transcript rows. The
worktree holds the seven modified files in §4 plus this report.
