# Claude Code ACP runtime — implementation plan

**Status:** Packet 0 complete (gate passed). Packet 1 complete after one
remediation pass against an independent review (findings F1–F9, F13, F15).
Packet 2 complete — see
[`2026-08-10-claude-code-acp-packet2-report.md`](2026-08-10-claude-code-acp-packet2-report.md).
Packet 3 not started.
**Branch:** `feat/claude-code-acp-runtime`
**Spike report:** [`2026-08-09-claude-code-acp-packet0-spike.md`](2026-08-09-claude-code-acp-packet0-spike.md)

## Goal

Claude Code is selectable as an **ordinary Hermes provider/model** in the CLI,
Discord, Telegram and every other surface. There is no user-facing `/acp`
workflow and no new user-visible concept. Behind that ordinary selection:

```
normal provider UX
  -> ClaudeCodeAcpRuntime (in-process delegated runtime)
    -> ACP client over stdio
      -> claude-agent-acp adapter
        -> Claude Code + Claude subscription
```

## Why ACP is a hard requirement

The earlier direct `claude` CLI / stream-json provider experiment failed with
hangs, leaked JSON/XML tool payloads in message text, disabled native tools,
stale subprocesses, weak permissions, and unreliable resume. ACP fixes these
structurally: tool activity arrives as typed `tool_call` / `tool_call_update`
events with structured `rawInput` / `rawOutput`, cancellation is a protocol
verb, permissions are a protocol request, and resume is `session/load`.
**Direct stream-json is not a fallback and is not to be reopened.**

`agent/copilot_acp_client.py` is the in-tree counter-example of the narrow
approach — a `ProviderTransport`-shaped ACP shim that spawns a process per
request, flattens the conversation into one prompt, and regex-parses
`<tool_call>` XML back out of the reply. We do not extend it.

## Ownership split (non-negotiable)

| Concern | Owner |
|---|---|
| Durable conversation id, transcript, gateway routing, turn lease, pending/delivery/crash recovery | **Hermes** |
| Ephemeral protocol state, adapter process, in-flight turn | **ACP** |
| ACP session id | A **discardable binding** on the Hermes session. No second recovery journal. |

One adapter process per active Hermes session; lazy spawn, long-lived
connection, idle eviction and retirement after a wedge.

## Load-bearing seams

Mirrors the `codex_app_server` delegated runtime, which is the accepted
precedent for this shape.

| Seam | File / line (at time of writing) | Change |
|---|---|---|
| Delegated-runtime early return | `agent/conversation_loop.py:1625` | Add a sibling branch for `api_mode == "claude_code_acp"` |
| Turn forwarder | `run_agent.py:8070` (`_run_codex_app_server_turn`) | Add `_run_claude_acp_turn` forwarder |
| Interrupt hook | `run_agent.py:3159` | Route interrupt to `session/cancel` |
| Steer hook | `run_agent.py:3348` | Decide: queue vs. `promptQueueing` capability |
| api_mode allow-list | `agent/agent_init.py:637` | Add `"claude_code_acp"` |
| Runtime module | new `agent/claude_code_runtime.py` | Analogue of `agent/codex_runtime.py` |
| Session adapter | new `agent/claude_acp_session.py` | Analogue of `codex_app_server_session.py` (**exists, Packet 2**) |
| Session binding + turn lease | new `agent/claude_acp_binding.py` | ACP session id + lease on the Hermes `sessions.model_config` row (**exists, Packet 2**) |
| Event projection | new `agent/transports/claude_acp_event_projector.py` | Analogue of `codex_event_projector.py` |
| Wire client | `agent/transports/claude_acp.py` | **Exists (Packet 0)** |
| Provider surface | `plugins/model-providers/claude-code-acp/__init__.py` | `register_provider(ProviderProfile(api_mode="claude_code_acp", auth_type="external_process"))`. NOT `claude-code/` — the `anthropic` profile already owns the `claude-code` alias. |
| Setup/install UX | `hermes_cli/model_setup_flows.py`, `hermes_cli/claude_acp_install.py` | Adapter presence check + managed install via `hermes model` (NOT `hermes tools`, which only toggles toolsets) |

`ProviderProfile.api_mode` (`providers/base.py:44`) is the field that makes
"an ordinary provider" resolve to a delegated runtime — no new user concept.

---

## Packet 0 — Feasibility spike ✅ COMPLETE

**Goal.** Prove a real prompt completes over ACP on subscription auth and
returns typed events, or stop and report the blocker.

**Scope.** Pin the adapter package/version/license; determine protocol version
and capabilities; prove `initialize` → `session/new` → `session/prompt` →
clean stop over real stdio; probe `session/load`, cancellation, permission,
filesystem capabilities non-destructively.

**Non-scope.** Any Hermes wiring. No provider, no runtime, no projection.

**Result.** Gate **passed** — see the spike report. Retained:
`agent/transports/claude_acp.py`, its 23 tests, `tests/.../_fake_acp_agent.py`,
`scripts/claude_acp_probe.py`. Adapter pinned at
`@agentclientprotocol/claude-agent-acp@0.66.0` (Apache-2.0), ACP protocol v1,
subscription auth confirmed with no `ANTHROPIC_API_KEY`. `session/load` was
additionally proven to survive an **adapter restart**.

**Stop condition.** Met. Proceed to Packet 1.

---

## Packet 1 — Provider surface + one real turn ✅ COMPLETE

**Goal.** Selecting Claude Code like any other model runs one complete turn
through the ACP runtime and renders a normal Hermes response.

**Permission posture (decided during remediation).** Packet 3 defers the
approval *UX*, never the *default*. Packet 1 therefore negotiates
`session/set_mode` → `default` immediately after `session/new` (leaving the
adapter's self-approving `auto`) and answers **every**
`session/request_permission` it receives with a denial. "No approval UX" must
mean deny, not silently allow. Packet 3 replaces the blanket denial with real
approvals.

**What that does and does not guarantee.** The guarantee is: *Hermes denies
every permission request the adapter asks it about.* It is **not** "nothing can
mutate the workspace". In Claude Code's `default` mode, tools already covered by
the user's own allow-lists (`~/.claude/settings.json`, project settings, session
`alwaysAllow`) are approved adapter-side and never produce a request Hermes can
see. A dogfooder with a permissive Claude config will therefore see some writes
succeed — that is the adapter honouring its own pre-existing policy, not the
denial being broken. Narrowing that gap needs adapter-side policy control and
belongs with the Packet 3 approval work.

**Scope.**
- Provider plugin under `plugins/model-providers/claude-code-acp/` pinning
  `api_mode="claude_code_acp"`; appears in `/model` and setup like any provider.
- Adapter resolution + presence check + managed install through the
  `hermes model` setup flow (`hermes_cli/claude_acp_install.py`), installing
  into the managed `~/.hermes/node` prefix. `hermes tools` is a toolset
  toggle UI and is deliberately NOT the install surface. Version pinned in
  one constant.
- `ClaudeCodeAcpRuntime`: lazy spawn, `initialize`, `session/new`, one
  `session/prompt`, assemble `agent_message_chunk`s into the final response,
  return through the delegated-runtime early return.
- Deterministic teardown: process reaped on turn end, error, and interpreter
  exit. No stale subprocess under any exit path.

**Non-scope.** Session reuse across turns (Packet 2). Rich event/permission
projection and streaming (Packet 3). Codex, Goose, generic adapters,
multi-agent pools, standalone bridge service, relay-connector northbound —
**out of MVP entirely**.

**Delegated-runtime parity (F8, resolved as parity).** The runtime fires the
same four end-of-turn hooks as `codex_runtime`: skill-iteration counter,
skill-nudge check, `_sync_external_memory_for_turn`, and
`_spawn_background_review`. Memory sync and background review are skipped on
interrupt/error so partial transcripts never reach memory. Without these,
selecting Claude Code would silently disable the agent-improvement loop for
the whole session.

**Usage accounting stays deferred** to Packet 3: `usage_update` events are
consumed but not projected, so this runtime contributes no token counts.

**Acceptance checks.**
1. A CLI turn against the Claude Code provider returns a correct answer with
   no JSON/XML tool payload leaking into the text.
2. `hermes` exits with zero surviving adapter processes **and no surviving
   descendants** — asserted after normal completion, error, and interrupt.
3. Missing adapter produces an actionable setup message, never a hang.
4. Non-ACP api_modes are byte-for-byte unaffected (existing suites green).
5. Hermes sends only the new user turn — it does **not** replay transcript
   history into the prompt (protects Claude Code's own prompt cache, R7).
6. The negotiated permission mode is the one Hermes requested, and a write
   attempt is denied and does not touch the workspace.
7. A cancel that races a normal completion still clears the interrupt latch,
   and turn N+1 works.
8. The adapter child never receives the **API-billing** credentials
   `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`, and **does** receive
   `CLAUDE_CODE_OAUTH_TOKEN`. That last one is minted by `claude setup-token`
   and IS the subscription credential — the documented way to authenticate
   Claude Code on a host with no interactive login, i.e. exactly the
   gateway/cron hosts this plan's goal statement names. Stripping it (as an
   earlier draft of this check required) would break the auth the provider
   advertises.
9. A non-`end_turn` stop reason yields `completed: False` and a non-empty
   assistant message (role alternation preserved).
10. `model.claude_code_acp.command` actually overrides resolution, and a
    Hermes-managed install under `$HERMES_HOME/node` is discovered.

**Safety invariants.** No new `HERMES_*` env var for non-secret config —
`model.claude_code_acp.command` in `config.yaml` is canonical and functional;
`HERMES_CLAUDE_ACP_COMMAND` remains a testing bridge. Credentials never
logged; adapter stderr reaches the user only as a bounded (≤5-line), redacted
diagnostic tail on failures. API-billing key vars are **stripped** from the
adapter env, while the `claude setup-token` subscription credential is
**preserved**, so the advertised subscription auth is both truthful and
functional on headless hosts. Every permission request Hermes receives is denied (see the
permission-posture note above for the exact scope of that guarantee).
Message-role alternation and cache rules unchanged.

**Stop condition.** Stop and report if one turn cannot complete without
replaying history, or if a stale process survives any exit path.

---

## Packet 2 — Session continuity ✅ COMPLETE

**Goal.** A Hermes conversation keeps one Claude Code session across many
turns, and survives adapter restart without a second recovery journal.

**Result.** Delivered as specified; every acceptance check below has a test
driving real adapter subprocesses. The binding lives in one `model_config`
key (`claude_acp`) on the Hermes `sessions` row — the same column that already
carries `browser_model_lock` / `yolo_mode` / `_delegate_from` — and carries the
turn lease with it. Adapter lifetime moved from the turn to the **session**:
`tests/agent/test_claude_code_runtime{,_safety}.py` had two reaping assertions
rewritten from "reaped at turn end" to "reaped at session teardown", which is
the contract this packet deliberately replaces. Full evidence, plus the
background-review interaction found on the way, is in the Packet 2 report.

**Scope.**
- Persist the ACP session id as a discardable binding on the Hermes session.
- Reuse the live connection across turns; on restart, `session/load`
  (capability-gated on `loadSession`).
- Fallback when `session/load` is unavailable **or fails**: `session/new` +
  bounded Hermes recap + exactly one visible reseed marker.
- Idle eviction and retirement after a wedge; one adapter per active session.
- Turn lease acquire/release across all exit paths, including crash.

**Non-scope.** Permission UX and full event projection (Packet 3).

**Acceptance checks.**
1. Multi-turn conversation: turn N+1 recalls context from turn N.
2. Kill the adapter mid-conversation → next turn transparently rebinds via
   `session/load` and still recalls context. (Proven feasible in Packet 0.)
3. Force the load path to fail → exactly one reseed marker, bounded recap, and
   the conversation continues.
4. Idle session is evicted; the next turn respawns; no leaked processes.
5. Killing Hermes mid-turn releases the lease; recovery does not double-deliver.
6. No ACP-side journal is written — Hermes' session store stays authoritative.

**Safety invariants.** Recap is bounded and never rebuilds the system prompt
mid-conversation. Exactly one reseed marker, user-visible. Lease released on
every path.

**Stop condition.** Stop and report if continuity requires Hermes to mirror
transcript state into an ACP-side store — that breaks the ownership split.

---

## Packet 3 — Event, permission, and cancellation projection

**Goal.** Tool activity, approvals, and cancellation behave like native Hermes
on every surface (CLI, Discord, Telegram).

**Scope.**
- Project `tool_call` / `tool_call_update` into Hermes' tool-activity display;
  `agent_message_chunk` into streaming output; `usage_update` into usage
  accounting.
- Bridge `session/request_permission` to Hermes' existing approval flow,
  including the gateway's two message guards (base adapter `_pending_messages`
  and the `gateway/run.py` runner) — both must be bypassed for approvals.
  **This replaces Packet 1's blanket denial**; the explicit `session/set_mode`
  → `default` negotiation already landed in Packet 1 and must stay.
- Interrupt → `session/cancel`; the turn must settle as `cancelled` and
  release its lease.
- Workspace containment via session `cwd` + `additionalDirectories`, **not**
  via `fs/*` interception (the adapter uses its own tools — spike §R4).

**Non-scope.** New core model tools. New user-facing commands.

**Acceptance checks.**
1. A tool-using turn shows structured activity, with no raw payload in text.
2. A write attempt raises a real approval prompt; denial blocks the write and
   the turn still completes cleanly. (Proven feasible in Packet 0.)
3. Approval prompts reach the user while the agent is running on a messaging
   platform.
4. `/stop` mid-turn cancels within a bounded time; `stopReason == "cancelled"`;
   the lease is released and the next turn works.
5. A test asserts the negotiated permission mode is the one Hermes requested —
   a silent `auto` default is a security regression.
6. Paths outside the workspace roots are refused
   (`ensure_path_within_roots`, already tested).

**Safety invariants.** Credential scrubbing on every projected event.
Permissions default to deny for every request Hermes receives. No approval
bypass introduced. Cancellation never
leaves an orphan process or a held lease.

**Stop condition.** Stop and report if permission requests cannot be delivered
through the gateway guards without a synthetic mid-loop user message — that
would break message-role alternation.

---

## Cross-cutting invariants (all packets)

- **Prompt caching is sacred.** Never rebuild the system prompt or replay past
  context mid-conversation. Claude Code owns its own history; Hermes sends the
  new turn only.
- **Strict role alternation.** No two same-role messages in a row; no synthetic
  user message injected mid-loop.
- **Credential scrubbing.** No token, key, or credential value is read, logged,
  printed, or projected — Packet 0 held this line and later packets must too.
- **Workspace containment.** Enforced through session roots and permission
  mode.
- **Deterministic teardown.** Every exit path reaps the adapter and releases
  the lease.
- **Dependency policy.** Zero new Python dependencies so far. The Node adapter
  is exact-pinned in one constant and installed through the existing managed
  Node path, not a raw env var.
- **Testing.** `scripts/run_tests.sh` only. CI coverage uses the fake ACP agent
  (no network, no quota); the live probe is run manually on version bumps.
