# Packet 1 report — Claude Code ACP runtime

**Date:** 2026-08-09
**Branch:** `feat/claude-code-acp-runtime`
**Status:** Closed after three review passes — an independent review (F1–F15),
a re-review that accepted Packet 1 with one High and four Mediums (H1, M1–M4,
L1/F10, L2, L4, L5), and a final delta review that returned CLOSE with one
Windows-only Medium and two Lows (M1-residual, L1 line-length, L2 cancel
re-issue). All are fixed.
**Plan:** [`claude-code-acp-runtime-plan.md`](claude-code-acp-runtime-plan.md)
**Spike:** [`2026-08-09-claude-code-acp-packet0-spike.md`](2026-08-09-claude-code-acp-packet0-spike.md)

Nothing committed, pushed, or deployed. No user config edited — every run used
an isolated temporary `HERMES_HOME`.

---

## 1. What Packet 1 delivers

Claude Code is selectable as an ordinary Hermes provider/model. Selecting it
routes the turn through an in-process delegated runtime to a real
`claude-agent-acp` subprocess authenticated with the user's Claude
subscription.

* ordinary provider/model registration, presence detection, interactive setup
* delegated-runtime early return (sibling of `codex_app_server`)
* one real ACP turn that sends **only the new user turn**
* `agent_message_chunk` text assembled into the normal Hermes response
* deterministic **process-tree** reaping on success / error / interrupt
* permissions negotiated off the adapter's self-approving default and denied
* delegated-runtime parity for the agent-improvement hooks

Held for later packets: session continuity and adapter reuse (Packet 2); rich
tool/thought projection, streaming, usage accounting, and the real approval UX
(Packet 3).

## 2. Remediation pass

An independent review rejected the first Packet 1 submission with 15 findings.
All mandated items are fixed; each has a test that failed before the fix.

| # | Finding | Resolution |
|---|---|---|
| F1 | Permission mode left at self-approving `auto`; no request handler | `session/set_mode` → `default` immediately after `session/new`; **every** `session/request_permission` denied via `deny_permission_response`; unknown agent requests refused explicitly |
| F2 | Adapter spawned as `claude-agent-acp --acp --stdio` (copilot's args leaked through a truthiness fallback) | Spec lookup is presence-based (`in extra`), so an explicit `()` means "no arguments" |
| F3 | Latched interrupt only cleared when `stopReason == "cancelled"` | Interrupt state now keys off Hermes' own latched flag, like `codex_runtime`; `partial` also honours `turn.cancel_requested` |
| F4 | `ANTHROPIC_API_KEY` et al. inherited into the adapter | API-billing credential vars are popped from the spawn env by default (`ANTHROPIC_CREDENTIAL_ENV_VARS`). **Amended by H1 below** — the list is two vars, not three |
| F5 | `model.claude_code_acp.command` inert; no managed install; pin duplicated | Config key is read (env → config → default); `find_node_executable` finds a managed `$HERMES_HOME/node` install; `hermes_cli/claude_acp_install.py` installs the pinned package into that prefix; the pin lives only in `hermes_cli.auth.CLAUDE_CODE_ACP_PACKAGE` and the probe imports it |
| F6 | `hermes model` → "Claude Code" saved nothing | `_model_flow_claude_code_acp` added and dispatched; persists provider + model + api_mode; offers the managed install; never blocks on stdin when there is no TTY |
| F7 | Only the direct child was killed | `_terminate_process_tree()` mirroring `_kill_git_process_tree`: POSIX `process_group=0` + `os.killpg` (pgid captured at spawn, since `getpgid` fails post-reap), `taskkill /T /F` on Windows; always swept, even after a clean exit |
| F8 | Memory / skill / background-review hooks dropped | **Parity taken.** `_run_improvement_hooks` fires the same four steps as `codex_runtime`; memory sync and background review skipped on interrupt/error |
| F9 | Empty text / non-terminal stop reasons reported as success | Non-`end_turn` stop reasons produce `completed: False`, a real `error`, and a non-empty assistant message so alternation holds |
| F13 | Spec defaults were copilot-shaped | All seven spec keys are required; a provider that omits one raises `ValueError` naming itself |
| F15 | 900 s timeout tore down without cancelling | `session/cancel` is sent before teardown on any prompt failure |
| F12 | `pgrep`/`os.kill(pid,0)` made reaping tests POSIX-only and flaky | Liveness via `psutil.pid_exists`; PID identified from the adapter's own record instead of a global `pgrep` sweep; process-group tests carry an explicit Windows skip, and the Windows tree-kill branch has its own unit test |

**F14 deliberately not taken** — six sibling surfaces (setup fallback models,
overlays, dashboard status card, model metadata, website docs); carried as an
open risk in §7. **F10 was taken in the hardening pass** (see §2b).

**F11** is informational; the plan's seam table has been corrected to name
`plugins/model-providers/claude-code-acp/` (the directory that exists).

## 2b. Hardening pass (post-acceptance, pre-dogfood)

The re-review accepted Packet 1 but flagged one High and four Mediums that
gate gateway/cron/headless and Windows use. All are fixed, each RED first.

| # | Finding | Resolution |
|---|---|---|
| **H1** | The remediation over-stripped `CLAUDE_CODE_OAUTH_TOKEN`. That is the token minted by `claude setup-token` — the **subscription** credential, and the documented way to authenticate Claude Code on a host with no interactive login. Stripping it breaks the auth this provider advertises, precisely on gateway/cron hosts. | `ANTHROPIC_CREDENTIAL_ENV_VARS` is now `("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")`. Two negative tests plus one positive test assert the child's own recorded environment; proven again through the real CLI (§6b). |
| **L1 / F10** | With H1 in place, an auth failure was the most likely first failure on a headless host, and surfaced as an opaque `ACP error …`. | `initialize` now inspects `auth_methods`; a non-empty list raises `ClaudeCodeAuthRequired` naming both remedies (`claude` login, or `claude setup-token` + `CLAUDE_CODE_OAUTH_TOKEN`). Failure messages carry **bounded (≤5 lines), redacted** adapter stderr via `_diagnostics()`; `stderr_tail()` finally has a caller. A test asserts a diagnostic line survives while an adjacent `sk-ant-…` secret does not. |
| **M1** | Windows tree-kill ran *after* `terminate()`/`kill()`. Killing the `.cmd` shim first makes `taskkill /T` unable to enumerate the `node.exe` grandchild — the exact leak F7 targets. The old test only asserted argv, so it passed while the semantics were wrong. | `taskkill /T /F` now runs **first inside `_terminate_process_tree`**, before any direct terminate/kill. The test records the **call sequence** and asserts `taskkill` precedes `kill`. **This fixed the helper only — see M1-residual in §2c for the caller.** |
| **M2** | Managed-adapter resolution went through `find_node_executable`, which spawns `<path> --version` and can trigger a **full managed-Node re-download** — from a provider *status* check reachable via `hermes doctor` and the dashboard. The old test stubbed the resolver, so the real path was never exercised. | New `_find_in_managed_node_dirs()` does a plain `iter_hermes_node_dirs()` scan with `shutil.which(path=…)` (keeps Windows PATHEXT, no subprocess). `claude_acp_install.adapter_path()` uses the same helper. Tests place a **real executable** in a temp `$HERMES_HOME/node/bin` with **no stubbing**, and one test fails outright if the Node healer or `--version` probe is reached. |
| **M3** | A cancel with zero streamed text appended no assistant row, leaving the transcript ending on a user message. | The empty-response substitution now covers the cancelled branch (`"(interrupted before Claude Code produced a response)"`). Tested with a `cancel-empty` adapter mode, plus an alternation check across cancelled → normal turns. |
| **M4** | "Permissions are denied outright" overstated the guarantee. | Plan and report now state it exactly: *Hermes denies every request it receives*; tools already covered by the user's own Claude allow-lists are approved adapter-side and never produce a request Hermes can see. |
| **L2** | Plan text still said `plugins/model-providers/claude-code/` and "install through `hermes tools`". | Both corrected; `hermes tools` is a toolset toggle UI and is explicitly not the install surface. A test asserts the install hint never says "hermes tools". |
| **L4** | `_pgid` fell back to the pid when `getpgid` failed, so the ownership guard could pass trivially against a recycled group. | `_pgid` stays `None` and the group kill is skipped. Two tests. |
| **L5** | The picker-wiring test used `raising=False` and would survive its subject being deleted. | `raising=False` removed. |

### A real bug found while fixing M3

Making the cancel tests deterministic exposed an intermittent hang that was
**not in the review**: `AIAgent.interrupt()` resolves `agent._claude_acp_turn`,
but an interrupt arriving *before* the runtime publishes that object found
`None` and was **silently lost** — the adapter then ran to the full 900 s turn
timeout while the user believed they had stopped it. The runtime now folds an
already-latched interrupt into the turn at publication time. Covered by
`test_interrupt_latched_before_the_turn_starts_is_not_lost`, which bounds the
turn at 60 s. The cancel tests were also made deterministic (they now wait for
the adapter to actually receive the prompt instead of racing a fixed sleep).

## 2c. Closure micro-fix pass

A final delta review returned **CLOSE PACKET 1** with no blocker and no High,
leaving one Windows-only Medium and two Lows. All three are now fixed.

| # | Finding | Resolution |
|---|---|---|
| **M1-residual** | The M1 ordering fix was complete inside `_terminate_process_tree` but **undone by its caller**: `close()`'s `TimeoutExpired` branch called `terminate()` first, killing the `.cmd` shim before `taskkill /T` could enumerate the surviving `node.exe`. Exactly the M1 leak, relocated from the helper to the caller — and invisible to the helper-level test. | On Windows, `close()` now goes straight to `_terminate_process_tree` in the wedged branch and never calls `terminate()` first. POSIX keeps its gentle `terminate()` → group-SIGKILL escalation, which is order-independent because `killpg` reaches the whole group. |
| **L1** | Diagnostics bounded line *count* (5) but not line *length* — one long JSON blob on a single stderr line would land whole in a Discord/Telegram message. | `_STDERR_DIAGNOSTIC_LINE_CHARS = 500`, applied **after** redaction (truncating first could split a secret into a prefix the redactor no longer matches). Two tests: overall truncation, and that the informative head of the line survives. |
| **L2** | A `session/cancel` delivered before any prompt existed may be legitimately ignored by a real adapter, so a pre-turn interrupt could still let the turn run to natural completion. | The client gained an `on_dispatched` hook that fires once the request is on the wire but before blocking on the reply. The runtime uses it to **re-issue** the cancel when one is already pending. Cancellation is idempotent and the turn still settles through the pending prompt reply. |

**Test-level evidence, not helper-level.** The M1-residual test drives a **real
wedged adapter subprocess** (new `wedged` fake mode that ignores stdin EOF)
through `close()` with Windows simulated, recording the actual teardown
sequence; it failed with `['terminate', 'taskkill', 'kill']` before the fix. A
companion test asserts POSIX teardown still reaps that same wedged adapter. The
L2 test uses a new `cancel-after-prompt-only` fake mode that discards any cancel
seen before the prompt handler starts — the precise behaviour the review
described — and bounds the turn at 60 s.

### A second timing race found and fixed

The two `ignore-cancel` tests still used a fixed `time.sleep(0.15)` before
interrupting. One run showed `test_second_turn_still_works_after_a_late_cancel`
as FLAKY: with turn one completing quickly, the interrupt could land in the
**second** turn and cancel that instead. Both now use
`_interrupt_once_prompt_lands`, and the second-turn test joins the interrupter
before starting turn two. Four consecutive runner passes, no FLAKY.

## 3. Changed files

### New (9)

| File | Purpose |
|---|---|
| `agent/transports/claude_acp.py` | ACP stdio client — wire layer, `set_mode`, env stripping, process-tree teardown |
| `agent/claude_code_runtime.py` | Delegated runtime: one turn, permission denial, parity hooks |
| `hermes_cli/claude_acp_install.py` | Managed install of the pinned adapter into `$HERMES_HOME/node` |
| `plugins/model-providers/claude-code-acp/{__init__.py,plugin.yaml}` | Provider profile + manifest |
| `scripts/claude_acp_probe.py` | Live capability probe (read-only; denies permissions) |
| `tests/agent/_fake_claude_acp_adapter.py` | Fake adapter subprocess (7 behaviour modes) |
| `tests/agent/test_claude_code_runtime.py` | 13 runtime integration tests |
| `tests/agent/test_claude_code_runtime_safety.py` | 17 remediation tests (F1/F3/F4/F7/F8/F9) |
| `tests/agent/transports/{test_claude_acp.py,_fake_acp_agent.py}` | 24 wire-client tests |
| `tests/hermes_cli/test_claude_code_acp_provider.py` | 17 provider-surface tests |
| `tests/hermes_cli/test_claude_code_acp_setup.py` | 18 resolution/setup tests (F2/F5/F6/F13) |

### Modified (8)

`agent/agent_init.py` (api_mode allow-list + provider→mode),
`agent/conversation_loop.py` (early return),
`run_agent.py` (turn forwarder + interrupt → `session/cancel`),
`hermes_cli/auth.py` (strict table-driven external-process spec, registry
entry, config + managed-node resolution, pin constant),
`hermes_cli/runtime_provider.py` (resolver branch),
`hermes_cli/models.py` (picker entry + pseudo-model),
`hermes_cli/model_setup_flows.py` (`_model_flow_claude_code_acp`),
`hermes_cli/main.py` (import + dispatch).

## 4. RED → GREEN

**RED (remediation), before any fix:**

```bash
scripts/run_tests.sh tests/agent/test_claude_code_runtime_safety.py \
                     tests/hermes_cli/test_claude_code_acp_setup.py \
                     tests/agent/transports/test_claude_acp.py
```

**32 failed** — 15 runtime-safety (F1, F3, F4, F7, F8, F9), 16
resolution/setup (F2, F5, F6, F13), 1 transport (`set_mode` missing).

The original Packet 1 RED, for the record, was **18 failed** with an
`APIConnectionError` proving `api_mode="claude_code_acp"` was not yet accepted.

**RED (hardening pass), before any fix:**

```bash
scripts/run_tests.sh tests/agent/test_claude_code_runtime_safety.py \
                     tests/hermes_cli/test_claude_code_acp_setup.py
# => 7 failed, 39 passed
```

The seven map exactly to H1, L1/F10 (×2), M1, M2, M3 and L4:

```
FAILED …TestAdapterChildEnvironment::test_subscription_setup_token_reaches_the_child
FAILED …TestAuthFailureDiagnostics::test_auth_required_produces_an_actionable_message
FAILED …TestAuthFailureDiagnostics::test_failure_includes_bounded_scrubbed_stderr
FAILED …TestWindowsTreeKill::test_taskkill_runs_before_the_direct_child_is_killed
FAILED …TestProcessGroupFallback::test_pgid_is_none_when_getpgid_fails
FAILED …TestNonSuccessStopReasons::test_cancelled_with_zero_text_still_appends_an_assistant_row
FAILED …TestManagedNodeResolution::test_status_check_never_probes_the_node_healer
```

**RED (closure micro-fix pass), before any fix:**

```bash
python3 -m pytest tests/agent/test_claude_code_runtime_safety.py -q --timeout=90
# => 3 failed, 28 passed
FAILED …TestWindowsTreeKill::test_close_does_not_terminate_the_shim_before_taskkill
FAILED …TestAuthFailureDiagnostics::test_long_diagnostic_lines_are_truncated
FAILED …TestInterruptLatchCleared::test_pre_prompt_cancel_is_reissued_once_the_prompt_is_in_flight
```

The M1-residual failure printed the reviewer's exact sequence:
`AssertionError: close() killed the shim before taskkill could walk the tree;
got ['terminate', 'taskkill', 'kill']`.

**GREEN:**

```bash
scripts/run_tests.sh tests/agent/test_claude_code_runtime.py \
                     tests/agent/test_claude_code_runtime_safety.py \
                     tests/hermes_cli/test_claude_code_acp_provider.py \
                     tests/hermes_cli/test_claude_code_acp_setup.py \
                     tests/agent/transports/test_claude_acp.py \
                     tests/providers/
# => 12 files, 156 tests passed, 0 failed
# run twice back-to-back: 0 failed, no FLAKY report
```

The safety suite (now 31 tests) was additionally run three times directly with
a 90 s per-test timeout, and four times through the runner after the
`ignore-cancel` race fix — 0 failed, no FLAKY, every time.

Lint: `ruff check` clean on all changed files;
`scripts/check-windows-footguns.py` clean on 9 files (it caught two real
`os.kill(pid, 0)` Windows footguns, now `psutil.pid_exists`).

## 5. Regression results

Latest (post-hardening) run:

| Command | Result |
|---|---|
| `scripts/run_tests.sh tests/hermes_cli/ tests/providers/` | **4763 passed, 1 failed** |
| `scripts/run_tests.sh tests/agent/ tests/run_agent/` | **5627 passed, 1 failed** |

Both failures are **pre-existing and unrelated**, previously confirmed by
reverting all tracked edits and removing the plugin directory:

* `tests/hermes_cli/test_doctor.py::test_doctor_reports_vercel_backend_diagnostics`
* `tests/agent/test_credential_pool_routing.py::TestFailureAttribution::test_unmatched_key_does_not_retry_only_pool_entry`
* `tests/agent/test_anthropic_output_field_leak.py` — collection error in
  `tests/conftest.py:1574` (`agent.moa_loop._preset_cache`), environment-level

`tests/run_agent/` and `tests/agent/transports/` remain at **0 failures**, so
every `codex_app_server` path is unaffected.

The closure pass re-ran the suites whose contract the changed client signature
touches:

```bash
scripts/run_tests.sh tests/agent/transports/ tests/run_agent/
# => 177 files, 1750 tests passed, 0 failed
```

**FLAKY files observed across the big parallel runs** (each failed once, passed
on retry), none of them caused by this packet:

| File | Why it is not ours |
|---|---|
| `tests/hermes_cli/test_config_read_guard.py` | A source-lint guard against raw `yaml.safe_load`; this packet reads config through the canonical `load_config()` owner, which is exactly what the guard requires. 3/3 in isolation. |
| `tests/agent/test_compression_worker_isolation_76354.py` | No compression file is touched by this diff. 3/3 in isolation. |
| `tests/run_agent/test_interrupt_propagation.py` | Contains **zero** `claude`/`acp` references, so the `api_mode == "claude_code_acp"` interrupt branch is inert there. 4/4 in isolation. Appeared only in a 1024 s run under heavy machine contention. |

Recorded as observed contention, not as a claim that these files are healthy.

## 6. Real end-to-end evidence

### 6a. Subscription auth completes with API-billing key vars SET

Hardening pass, real adapter, real subscription. `CLAUDE_CODE_OAUTH_TOKEN` is
deliberately left untouched here so real auth is not disturbed.

```bash
env ANTHROPIC_API_KEY="sk-ant-SENTINEL-not-a-real-key-000" \
    ANTHROPIC_AUTH_TOKEN="SENTINEL-auth-token-000" \
    HERMES_HOME=<temp> PYTHONPATH=<repo> \
    HERMES_CLAUDE_ACP_COMMAND=<...>/claude-agent-acp \
    python3 hermes_cli/main.py --provider claude-code-acp -m claude-code \
      -z "Read fact.txt and reply with only the number in it."
# => 24601
```

### 6a-prev. Earlier remediation-pass run (three vars set)

Sentinels are obviously-fake placeholders. If any had reached the adapter it
would have attempted API-key auth; instead subscription auth completed.

```bash
env ANTHROPIC_API_KEY="sk-ant-SENTINEL-not-a-real-key-000" \
    ANTHROPIC_AUTH_TOKEN="SENTINEL-auth-token-000" \
    CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-SENTINEL-000" \
    HERMES_HOME=<temp> PYTHONPATH=<repo> \
    HERMES_CLAUDE_ACP_COMMAND=<...>/claude-agent-acp \
    python3 hermes_cli/main.py --provider claude-code-acp -m claude-code \
      -z "Read fact.txt and reply with only the number in it."
# => 8675309
```

Correct answer, read by Claude Code's own native tools, no tool payload in the
text. **No real token was printed at any point.**

### 6b. The adapter child provably does not receive them

Same real CLI and provider-resolution path, with the fake adapter recording its
own environment:

Post-H1, with all three sentinels set in the parent — obvious test sentinels
only, never a real token:

```
ANTHROPIC_API_KEY        : None
ANTHROPIC_AUTH_TOKEN     : None
CLAUDE_CODE_OAUTH_TOKEN  : sk-ant-oat01-OBVIOUS-TEST-SENTINEL
set_mode_calls           : ['default']
```

That is exactly the H1 contract: the two API-billing credentials are stripped,
the subscription setup-token propagates so headless auth can work. The same
record proves the permission mode is negotiated on the live path, not only in
unit tests.

### 6c. Missing adapter — actionable, no hang

```
hermes -z: agent failed: Could not find the Claude Code ACP adapter
'claude-agent-acp-not-installed'. Run `hermes model` and pick Claude Code to
install it into the Hermes-managed Node prefix, or set
model.claude_code_acp.command in config.yaml.
```

Both remedies now work; the previous message named an inert config key.

### 6d. No surviving processes

```bash
pgrep -af "claude-agent-acp"          # => none
pgrep -af "_fake_claude_acp_adapter"  # => none
pgrep -af "time.sleep(600)"           # => none  (grandchild probe)
```

## 7. Open risks

| # | Risk | Next step |
|---|---|---|
| R1 | Per-turn adapter spawn (~1–2 s handshake per turn); no cross-turn context | Packet 2 |
| R2 | **Hermes denies every permission request it receives** — a turn needing a write will refuse and say so. This is NOT "nothing can mutate the workspace": in Claude Code's `default` mode, tools already covered by the user's own allow-lists (`~/.claude/settings.json`, project settings, session `alwaysAllow`) are approved adapter-side and never generate a request Hermes can see. A dogfooder with a permissive Claude config will see some writes succeed. | Packet 3 replaces the blanket denial with real approvals; narrowing the allow-list gap needs adapter-side policy control |
| R3 | No usage/token accounting — `usage_update` consumed and dropped | Packet 3 |
| R4 | **F14 not taken:** setup fallback models, provider overlays, dashboard status card, model metadata, and website docs still omit this provider | Follow-up polish |
| R5 | Windows teardown is now tested for argv and call ordering at **both** the helper and `close()` levels, against a real wedged adapter with Windows simulated — but it is still never executed on a real Windows host; CI is ubuntu-only. `taskkill` itself has never run. | Needs a Windows CI lane or a manual pass before Windows dogfood |
| R6 | Auth-required detection keys off a non-empty `auth_methods` at `initialize`. An adapter that instead accepts `initialize` and fails later still yields the generic failure — now with bounded scrubbed stderr attached, but no tailored remedy | Revisit if the adapter's behaviour changes |
| R7 | Live cancellation verified against the fake adapter only — including the pre-turn-interrupt fix, which is bounded by a 60 s test rather than exercised against the real adapter | Packet 3 |
| R7b | Headless subscription auth (`CLAUDE_CODE_OAUTH_TOKEN` only, no `~/.claude` credentials) is proven for **env propagation** but has not been run end-to-end against a real headless host — doing so would require a real setup-token, deliberately not used here | Verify on the first gateway/cron host |
| R8 | Adapter is pre-1.0 (0.66.0) and was renamed once | Exact pin + `protocolVersion == 1` assertion; re-run the probe on bumps |
| R9 | The live E2E cannot run in CI (real subscription, consumes quota) | Fake-adapter tests are the CI contract |
| R10 | Pseudo-model `claude-code` is a placeholder; the real model is chosen inside the session | Consider projecting `session_info_update` in Packet 3 |
| R11 | Pre-existing repo noise: 2 failing tests + 1 conftest collection error, all proven unrelated | Separate fix |

## 8. Stop point

Packet 1 is complete and evidenced. **No Packet 2 work started.** Next
increment is session continuity: durable ACP-session binding, `session/load`
reattach, bounded recap + a single visible reseed marker, idle eviction, and
turn-lease release across all exit paths.
