# Claude Code ACP runtime — implementation plan

**Status:** Packet 0 complete (gate passed). Packet 1 complete after one
remediation pass against an independent review (findings F1–F9, F13, F15).
Packet 2 complete — see
[`2026-08-10-claude-code-acp-packet2-report.md`](2026-08-10-claude-code-acp-packet2-report.md).
Packet 3 **slices 3A (event projection), 3B (permission bridging) and 3C
(cancellation settling + workspace containment) complete** — see
[`2026-08-10-claude-code-acp-packet3a-report.md`](2026-08-10-claude-code-acp-packet3a-report.md),
[`2026-08-10-claude-code-acp-packet3b-report.md`](2026-08-10-claude-code-acp-packet3b-report.md)
and [`2026-08-10-claude-code-acp-packet3c-report.md`](2026-08-10-claude-code-acp-packet3c-report.md).
Packet 3 was reviewed independently
([review](2026-08-10-claude-code-acp-packet3-review.md)) and its four required
fixes plus two recommendations landed in a follow-up pass
([fixes report](2026-08-10-claude-code-acp-packet3-fixes-report.md)).
Packet 4 **slices A (model identity capture/display) and B (user-driven model
selection through `/model`) complete** — picks up Packet 1's R10 ("pseudo-model
`claude-code` is a placeholder"), left open through Packet 3. See the Packet 4
section below.

Transcript-level tool rows (3A's R2) remain the one deferred Packet 3 item, and
"deferred" has a concrete cost worth stating rather than closing by silence:
`agent/claude_code_runtime.py` never splices `(assistant tool_calls, tool)` pairs
into `messages` the way `agent/codex_runtime.py` does, so `agent/curator.py`,
external-memory review, background-review snapshots and `session_search` all see
a Claude Code turn as text with **no tool activity at all**. Packet 3's scope
bullet says *display*, so this is not an acceptance failure — it is a parity gap
with the sibling delegated runtime, scheduled, not closed. Two further items the
review left open are also not implemented: a Hermes-side post-cancel grace
deadline (3C R3 / review L1) and any adapter-side pre-approved-tool policy
(3B R1 / 3C R1 / review M3).
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
| Steer hook | `run_agent.py:3348` | **Decided (Packet 3 fixes):** queue. `_steer_handoff` drains `_pending_steer` into `result["pending_steer"]`, the existing contract all three surfaces already deliver as the *next user turn* — no mid-loop injection, so role alternation holds, and no dependence on the adapter's `promptQueueing` capability |
| api_mode allow-list | `agent/agent_init.py:637` | Add `"claude_code_acp"` |
| Runtime module | new `agent/claude_code_runtime.py` | Analogue of `agent/codex_runtime.py` |
| Session adapter | new `agent/claude_acp_session.py` | Analogue of `codex_app_server_session.py` (**exists, Packet 2**) |
| Workspace roots | new `agent/claude_acp_workspace.py` | **Exists (Packet 3C)** — resolves `cwd` + explicitly-allowed `additionalDirectories` from `model.claude_code_acp.additional_directories`, and owns the root check every agent-initiated path is measured against |
| Session binding + turn lease | new `agent/claude_acp_binding.py` | ACP session id + lease on the Hermes `sessions.model_config` row (**exists, Packet 2**) |
| Event projection | new `agent/transports/claude_acp_event_projector.py` | Analogue of `codex_event_projector.py` (**exists, Packet 3A**), consumed through the `event_sink` seam on `ClaudeAcpSession.run_turn` and the bridge in `claude_code_runtime.make_claude_acp_event_bridge` |
| Wire client | `agent/transports/claude_acp.py` | **Exists (Packet 0)** |
| Permission bridge | `tools/approval.py::request_delegated_runtime_approval` | **Exists (Packet 3B)** — reuses the shared `_run_approval_gate` decision core; reached through the `permission_handler` seam on `ClaudeAcpSession.run_turn` and `claude_code_runtime.make_claude_acp_permission_bridge` |
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

**Progress.** Slices **3A (event projection)**, **3B (permission bridging)** and
**3C (cancellation settling + workspace containment)** are complete — see the
[Packet 3A report](2026-08-10-claude-code-acp-packet3a-report.md) and the
[Packet 3B report](2026-08-10-claude-code-acp-packet3b-report.md). 3A delivered
the first scope bullet below (tool activity, streaming, usage) with credential
scrubbing on every projected *display* field (the two raw, never-rendered
policy-only fields are scoped under "Cross-cutting invariants" below), and two
decisions worth carrying forward —
per-turn tokens come from the `session/prompt` reply while `usage_update`
carries context occupancy (never summed), and Hermes' context compressor is
deliberately not driven on this runtime (closes Packet 2 R5). 3B replaced the
blanket denial with Hermes' own approval gate on every surface, keeping deny as
the default on every path that cannot produce a human answer, and never handing
the adapter a standing grant. 3C settled cancellation (one delivery, lease
released, pending approvals fail closed, nothing left running) and made
workspace containment a negotiated fact — `cwd` plus only the directories named
in `model.claude_code_acp.additional_directories`, capability-gated, re-sent on
rebind, and the yardstick for every agent-initiated filesystem request. Still
open: transcript-level tool rows for curator/memory (3A's R2).

**Scope.**
- Project `tool_call` / `tool_call_update` into Hermes' tool-activity display;
  `agent_message_chunk` into streaming output; `usage_update` into usage
  accounting. ✅ **slice 3A**
- Bridge `session/request_permission` to Hermes' existing approval flow,
  including the gateway's two message guards (base adapter `_pending_messages`
  and the `gateway/run.py` runner) — both must be bypassed for approvals.
  **This replaces Packet 1's blanket denial**; the explicit `session/set_mode`
  → `default` negotiation already landed in Packet 1 and must stay.
  ✅ **slice 3B**
- Interrupt → `session/cancel`; the turn must settle as `cancelled` and
  release its lease. ✅ **slice 3C**
- Workspace containment via session `cwd` + `additionalDirectories`, **not**
  via `fs/*` interception (the adapter uses its own tools — spike §R4).
  ✅ **slice 3C**

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

**Safety invariants.** Credential scrubbing on every *projected display* field —
every tool/permission field that reaches a transcript, a display surface or a
chat message. Permissions default to deny for every request Hermes receives. No
approval bypass introduced. Cancellation never leaves an orphan process or a held
lease.

**Stop condition.** Stop and report if permission requests cannot be delivered
through the gateway guards without a synthetic mid-loop user message — that
would break message-role alternation.

---

## Packet 4 — Model identity and selection

**Goal.** Hermes' runtime/usage/footer surfaces show the model Claude Code is
*actually* running, not the static pseudo-model
(`plugins/model-providers/claude-code-acp`'s `"claude-code"`) carried since
Packet 1. Packet 1's R10 named this a placeholder and suggested projecting
`session_info_update` in Packet 3; that never landed (`session_info_update`
turns out to be the wrong message — see slice A below for the one that
actually carries it), so the gap survived through Packet 3 unaddressed.

Slice A makes Hermes *observe* that model; slice B lets the user *pick* it from
the live session's own advertised list through the ordinary `/model` command, on
every surface. Both read the same ACP mechanism (`configOptions`) and land in
the same captured identity, so a pick is visible on the footer/usage surfaces
immediately and no second mechanism exists to keep in sync.

### Slice A — state capture / display / usage identity ✅ COMPLETE

**Scope.**
- Capture the model state ACP reports and keep it current for the life of a
  Hermes conversation, surviving reconnects and adapter restarts.
- Replace the pseudo-model on read-only display/usage surfaces: the CLI
  status-bar footer, `/usage`'s "Model:" line, the desktop/TUI gateway's
  `session.info` `"model"` field, and (added when slice B gave the gateway a
  `/model`) the messaging gateway's `/usage`, `/context` and `/agents`.
- Preserve verbatim whatever ACP reported, and key internal accounting
  (SessionDB usage rows, pricing, context-window metadata) on the model id
  *when ACP reported an id*, while showing a concise normalized label (e.g.
  `sonnet-5` for `claude-sonnet-5-20260815`) on the display surfaces above.
  See "The selector is not always an id" below for the case where it did not.
- Capture automatic, adapter-driven model changes (the Claude Agent SDK's own
  refusal-fallback) the same way as any other identity update — no separate
  mechanism, since the wire gives none.

**Non-scope.** User-driven model switching and `/model` UX for this provider
(slice B) — nothing in slice A lets Hermes pick or change the underlying
model, only observe it. Hermes' own cross-provider `_try_activate_fallback` /
`fallback_model` chain gaining a `claude_code_acp` branch (its `fb_api_mode`
table has none today) — a separate, larger change to a different mechanism,
left for whichever packet actually wires Claude Code into a fallback chain.
Mutating `agent.model` itself — see the safety invariant below for why that
is not merely deferred but actively wrong for this runtime.

**What ACP actually exposes, and why the obvious approach doesn't apply.**
ACP has no dedicated "current model" field or change notification. The
`acp` schema's `NewSessionResponse.models` (`SessionModelState` /
`currentModelId`) exists but the pinned adapter
(`@agentclientprotocol/claude-agent-acp@0.66.0`) does not populate it —
confirmed by reading `dist/acp-agent.js`'s `createSession()`, whose actual
return is `{ sessionId, modes, configOptions }`, no `models` key. The
identity travels instead inside the generic `configOptions` list (`select`
options for mode/model/effort/fast-mode), in the entry with `id: "model"`,
whose `currentValue` is the value that entry's *picker* currently has selected
(`buildConfigOptions` / `MODEL_CONFIG_ID` in `dist/acp-agent.js`) — a model id
much of the time, but not always one; see "The selector is not always an id".
`configOptions` is present on `session/new`'s and `session/load`'s responses
**and** on every `config_option_update` notification — and critically, the
adapter's own SDK-side refusal fallback
(`syncModelAfterRefusalFallback`) re-uses that exact same
`config_option_update` path a user-driven `/model` switch would take, so one
extractor, read at both the response and the notification, covers the
initial identity, a resumed session's identity, a manual switch, and an
automatic one — with no separate "fallback happened" signal to build against.
`extract_current_model_id()` in `agent/transports/claude_acp_event_projector.py`
is that one extractor, shared by the wire client's `current_model_id`
property and the projector's `config_option_update` handling.

**The selector is not always an id.** The first pass through this treated
`currentValue` as the authoritative model id everywhere — display *and*
accounting. Re-reading the adapter and the SDK together shows it is the
picker's selected *value*, and in the most ordinary configuration that value
is literally `"default"`:

- `ModelInfo.value` is documented as possibly an alias, with the canonical
  wire id in a separate `resolvedModel` field
  (`@anthropic-ai/claude-agent-sdk`'s `sdk.d.ts`) — and `buildConfigOptions`
  maps only `value`/`name`/`description` onto ACP, so `resolvedModel` never
  leaves the adapter. Hermes cannot recover it.
- `applyAvailableModelsAllowlist` unconditionally prepends a `"default"` row,
  and `getAvailableModels` selects `models[0]` whenever neither
  `ANTHROPIC_MODEL` nor `settings.model` is set — i.e. for any user with an
  `availableModels` allowlist and no explicit model, which is exactly the
  configuration slice B's allowlist story rests on.

So the identity carries the raw value verbatim (a pick and any wire comparison
need it) plus an `is_authoritative` predicate, and the two consumers diverge:

- **Display** (`claude_acp_display_model`) shows the id shortened when it is an
  id, and otherwise the *name* the session advertised for that row (`default` →
  `Default`, `sonnet` → `Sonnet 5`), never the bare token — "the model is
  `default`" reads as a model name and is not one. A row that names nothing gets
  the token capitalized instead, and "names nothing" covers both spellings the
  wire can produce: no `name` at all, and a `name` that is the value echoed back
  (`applyAvailableModelsAllowlist`'s no-SDK-match branch sets `displayName` to
  the value, so this is the *reachable* one). `extract_model_options()` reports
  an absent name as `""` rather than substituting the value, and
  `_advertised_name()` is the single place both spellings collapse — without
  that, the fallback would be unreachable from the wire and the invariant only
  aspirational.
- **Accounting** (`claude_acp_usage_model`, feeding the cost estimator, the
  `session_model_usage` row and `/context`'s window lookup) uses the id when
  there is one and otherwise falls back to `agent.model`, the provider's
  pseudo-model. Persisting `"default"` as though it were a model would invent a
  row nothing can be reconciled against — the opposite of what per-model
  attribution (issue #51607) exists for — while the pseudo-model is both honest
  ("not reported") and the value pre-Packet-4 rows for this provider already
  carry. Billing is unaffected either way: `resolve_billing_route` keys on
  provider.

**Why `agent.model` was not the answer.** The obvious fix — set `agent.model`
to the live identity — was tried on paper and rejected: `cli.py`'s status bar
already prefers `agent.model` over the CLI's own configured value specifically
because *other* providers' `_try_activate_fallback()` mutates it in place, so
mirroring that would have looked consistent. But `gateway/run.py` runs a
post-turn check on every successful messaging-platform turn —
`_agent.model != _cfg_model` evicts the cached agent (forcing a respawn) unless
`_is_intentional_model_switch()` recognizes an active `/model` override. A
Claude Code turn's `agent.model` would then differ from the configured
`"claude-code"` on *every single turn* once real identity landed, which is not
an intentional switch by that check's definition — so every successful
gateway/Discord/Telegram turn would evict and respawn the adapter, destroying
Packet 2's session/process reuse. The identity therefore lives on its own
attribute (`agent._claude_acp_model_identity`, a frozen `ClaudeAcpModelIdentity`
carrying both the exact `model_id` and the normalized `label`), read through
`claude_acp_model_identity()` / `claude_acp_display_model()` in
`agent/claude_code_runtime.py` rather than by touching `agent.model` at all.

**Seams landed.**

| Seam | File | What it does |
|---|---|---|
| Wire capture | `agent/transports/claude_acp.py` | `session_new()` / `session_load()` stash the response's `configOptions`; `ClaudeAcpClient.current_model_id` reads the "model" entry back out |
| Shared extractor | `agent/transports/claude_acp_event_projector.py` | `extract_current_model_id()` — one function, used by the wire client and by... |
| Live-change projection | `agent/transports/claude_acp_event_projector.py` | `ClaudeAcpEventProjector._project_config_option_update()` — a `config_option_update` whose model entry changed value projects as `kind="model_changed"`; a restatement (an unrelated mode/effort change re-sends the same value) is deduped and ignored *within a turn* — the projector is per-turn and starts empty, so the first update of a turn projects even if it restates the current model, which costs nothing (no display surface; the session writes back the value it held) |
| Session-lifetime identity | `agent/claude_acp_session.py` | `ClaudeAcpSession._current_model_id` / `.current_model_id` — the LIVE adapter's answer: re-seeded unconditionally at every `_connect()` from that connection's own response, updated in `run_turn()` from the projector, dropped in `_drop_client()` with the adapter that reported it; `AcpTurnOutcome.model_id` carries it out per turn. `_remembered_model_id` / `_remembered_model_options` keep the last reported state for *recognition* after teardown (slice B's idle-adapter refusal), and are never offered as a live list |
| Normalization + accessors | `agent/claude_code_runtime.py` | `normalize_claude_model_label()` (strips the `claude-` vendor prefix and a trailing 8-digit release-date suffix, keeping a `[1m]` context-lane marker), `_is_authoritative_model_id()` / `_option_label()` / `_selector_label()` (id vs picker selector — see above), `ClaudeAcpModelIdentity` (`.model_id` verbatim, `.label`, `.is_authoritative`), `claude_acp_model_identity(agent)`, `claude_acp_display_model(agent)`, captured post-turn by `_capture_model_identity()` |
| Usage accounting | `agent/claude_code_runtime.py` | `claude_acp_usage_model()` (still reachable as `_usage_model_label()`) feeds the exact raw id — not the normalized label — to `estimate_usage_cost()` and both `_queue_token_counts()` call sites, falling back to `agent.model` (`"claude-code"`) before any identity is known AND when the reported value is a selector rather than an id |
| Gateway displays | `gateway/slash_commands.py` | `_display_model_for()` / `_metadata_model_for()` — the same preference on `/usage`, `/context` and `/agents`; `/context` sizes its window from the accounting id, not the label. Added with slice B, which is what gave the gateway a `/model` to disagree with |
| CLI footer | `cli.py` (`_get_status_bar_snapshot`) | Prefers `claude_acp_display_model(agent)` ahead of `agent.model`/`self.model` |
| CLI usage report | `cli.py` (`_show_usage`) | Same preference for the "Model:" line |
| Desktop/TUI footer | `tui_gateway/server.py` (`_session_info`) | Same preference, ordered after a pending `/model` switch (more specific and more recent) but ahead of the mirror/agent fallback |

The event bridge (`make_claude_acp_event_bridge`'s `sink()`) gets a
`model_changed` no-op branch alongside `tool_updated`/`usage`: this is state
capture only, and deliberately produces no chat bubble or approval-style
prompt — the session/runtime read the identity straight off
`AcpTurnOutcome.model_id` rather than through the display bridge, keeping
slice A's "no new user-facing UX" boundary structural rather than a
convention someone has to remember.

**Acceptance checks.**
1. After one completed turn, `claude_acp_display_model(agent)` returns the
   concise normalized label and `claude_acp_model_identity(agent).model_id`
   is exactly what ACP reported, unmodified — whether that is a model id or a
   picker selector, with `.is_authoritative` saying which.
2. `agent.model` is bit-for-bit unchanged by a Claude Code turn.
3. A mid-turn `config_option_update` reporting a new model (the adapter's own
   refusal fallback) updates both the display label and the raw id, with no
   new callback fired on any Hermes display surface.
4. SessionDB usage rows and the cost estimator receive the exact raw
   authoritative id, not `"claude-code"`, once one is known — asserted on the
   persisted `session_model_usage` row, not only on the estimator's argument.
   When ACP reported a selector rather than an id, they receive the
   pseudo-model instead and never the selector. The turn is priced
   `cost_status == "included"` in both cases (`resolve_billing_route` keys on
   `provider`, not `model` — confirmed unaffected).
5. The CLI status bar, CLI `/usage`, the desktop/TUI `session.info.model`
   field and the gateway's `/usage` / `/context` / `/agents` all show the
   label once known, and the unchanged pseudo-model beforehand or on a non-ACP
   agent. The label is never a bare picker token (`default`).
6. Existing Packet 0–3 suites (312 tests across
   `tests/agent/test_claude_code_*.py`,
   `tests/agent/transports/test_claude_acp*.py`,
   `tests/gateway/test_claude_code_acp_approval_guards.py`,
   `tests/hermes_cli/test_claude_code_acp_*.py`) stay green — this slice adds
   a new attribute and new read sites, and touches no existing call's
   behavior for a non-Claude-Code agent.

**Safety invariants.** `agent.model` is never mutated by this runtime (see
above — it is a correctness requirement, not a style choice: gateway agent
caching keys eviction off it). Prompt caching, role alternation, credential
redaction, session continuity and every other provider are unaffected — the
only new reads are additive `getattr`/property lookups guarded by `try/except`
at each display call site, so a lookup failure degrades to the pre-Packet-4
behavior rather than breaking the surface it touches.

**Stop condition.** Met — no blocker: the mechanism ACP actually offers
(`configOptions`) covers every case slice A needed (initial identity, resumed
identity, automatic change), which the earlier `session_info_update` guess
would not have.

### Slice B — user-driven model selection ✅ COMPLETE

**Goal.** Selecting which model Claude Code runs is provider-like and lives in
Hermes' existing `/model`, on every surface that has one. No new command, no
new user concept, and no adapter restart: the pick is applied to the ACP
session already serving the conversation.

**Scope.**
- List the models the **live ACP session** actually advertises, mark the one
  it is running, and offer nothing when it advertises none.
- Apply a pick with `session/set_config_option` on that same session — same
  adapter process, same ACP session id, same binding, same negotiated roots and
  permission mode.
- Wire it into `/model` on the CLI and the Discord/Telegram gateway through one
  shared seam, so the two cannot drift and neither grows a platform-specific
  command.
- Let slice A's captured identity carry the result: the footer, `/usage` and the
  desktop/TUI `session.info.model` are current the moment the pick lands.

**Non-scope.** The effort / permission-mode / agent selectors the same
`configOptions` list carries (`session/set_config_option` is generic; only the
`model` option is wired, and the wire client's docstring says why the
permission mode must keep going through `set_mode` instead). Persisting a pick
across conversations — see the scope-flag decision below. The desktop/TUI's own
`/model` surface (`tui_gateway`'s queued `pending_model_switch`, a turn-boundary
mechanism of its own); its footer already follows a pick made elsewhere via
slice A. **That divergence has a user-visible consequence worth stating rather
than leaving implicit:** the same words (`/model haiku-4-5`) switch in place on
the CLI and the messaging gateway, and on the desktop/TUI take the route-switch
path — which moves the conversation off Claude Code entirely and retires the
adapter. The "the two cannot drift" claim below is about the two surfaces this
slice wires, not all three. Hermes' cross-provider `_try_activate_fallback`
chain, unchanged from slice A's non-scope note.

**The wire, corrected.** Slice A's stub for this slice named
`availableModels` + `session/set_model`; both were wrong for the pinned adapter,
and re-reading `dist/acp-agent.js` (0.66.0) before writing anything is what
found it. `SessionModelState` / `session/set_model` are **UNSTABLE** in the ACP
schema *and* unpopulated by this adapter — the same reason slice A had to read
`configOptions`. Selection travels through the same generic option: the "model"
entry's `options` list (`buildConfigOptions`, from `models.availableModels`),
written with **`session/set_config_option`** — params `{sessionId, configId,
value}` (note `configId`, not `optionId`), reply `{configOptions}` carrying the
whole refreshed list. The adapter canonicalises the value it applies
(`resolveModelPreference` accepts aliases), so the reply is read back rather
than the request assumed, and unlike its own refusal fallback the RPC path
sends **no** accompanying `config_option_update` — so a user-driven pick and an
automatic one stay distinguishable while both land in the same captured state.

**Capability gating, answered.** There is no ACP capability for this: adapter
0.66.0's `initialize` advertises none, and the client capability that exists
(`session.configOptions.boolean`) is about boolean options, not model
selection. The gate is therefore **the advertised list itself** — an empty
`options` list means no pick is offered here, on both the read and the write
path. That also answers the allowlist question slice A left open: the list the
adapter advertises is already filtered by the user's own
`~/.claude/settings.json` `availableModels`, so honouring it verbatim keeps a
Hermes-side pick inside the adapter's policy instead of second-guessing it.

**Scope flags, decided.** `--global` / `--once` are **refused by name**, not
silently ignored. A Claude Code pick is state inside the live ACP session, and
Hermes has nowhere to persist it as a *route*: `model.default` must stay the
provider's pseudo-model or provider resolution and the gateway's agent-cache
drift check both break (slice A's core finding). Bare `/model <name>` and
`--session` are the supported forms, and the copy says the pick applies to this
conversation.

**Fall-through is the safety property, up to the point of recognition.** The
seam answers only what it owns and returns `None` otherwise — a non-Claude-Code
agent, a conversation with no session yet, a live session that reports no model
state at all, `--provider <slug>` (the way *out* of Claude Code), and any name
this session does not advertise, so `/model gpt-5.5` from a Claude Code session
still switches provider exactly as before. Both call sites also wrap the seam in
`try/except` and fall through on any fault, so a fault raised *before* the seam
recognises the name degrades to the pre-slice-B behaviour.

Past that point the seam deliberately does **not** fall through: once the name
is known to be one of this session's models, an internal fault is caught inside
the seam and answered as a refusal (`hermes_cli/claude_acp_model_select.py`'s
broad `except Exception`), so the call sites' `try/except` never sees it. That
is the safer of the two: falling through with a name we know is a Claude Code
model would hand it to the cross-provider resolver, silently moving the
conversation and its billing route to another provider. Refusing says the switch
did not happen, which is true.

**One exception to fall-through, and why.** Packet 2 retires an adapter that
crashed, was replaced, or simply went **idle** (`IDLE_TTL_SECONDS`), leaving the
session object alive and still remembering the list it advertised. A pick cannot
be applied there — the model is state inside the adapter — but falling through
with a name we *know* is a Claude Code model would silently move the
conversation, and its billing route, onto another provider. So a recognised name
is refused with "send a message first, then pick a model", an unrecognised one
still falls through, and a bare `/model` still opens the ordinary picker rather
than presenting a stale list as live. Before the very first turn nothing is
remembered, so a Claude Code model is indistinguishable from any other name and
`/model` behaves exactly as it always did — the only non-guessing option there.

**Seams landed.**

| Seam | File | What it does |
|---|---|---|
| Advertised-list extractor | `agent/transports/claude_acp_event_projector.py` | `extract_model_options()` — the "model" entry's `options`, groups flattened as the adapter flattens them; sibling of slice A's `extract_current_model_id()`, sharing one `_model_config_option()` finder. Reports a missing `name` as `""` (the fact display code needs) rather than copying the value into it |
| Wire read | `agent/transports/claude_acp.py` | `ClaudeAcpClient.available_model_options` — `None` when the last session response carried no model entry at all, `[]` when it carried one advertising nothing. Two different facts, and a reconnect has to tell them apart (`has_model_config_option()` is the predicate) |
| Wire write | `agent/transports/claude_acp.py` | `ClaudeAcpClient.set_config_option()` — issues `session/set_config_option` and replaces the local `configOptions` capture from the reply; a rejection raises and updates nothing |
| Session state | `agent/claude_acp_session.py` | `model_options` / `remembered_model_options` / `turn_in_flight` / `set_model()`, plus `_capture_model_state()` — one place that refreshes identity **and** option list from a response, used by `_connect()` too, and re-seeding *unconditionally* so a rebind against a different adapter build cannot inherit the previous one's list |
| Refusal type | `agent/claude_acp_session.py` | `ClaudeAcpModelSelectionError` — no live session, no advertised models, unadvertised model, turn in flight, or the adapter's own refusal (reason preserved) |
| Runtime accessors | `agent/claude_code_runtime.py` | `ClaudeAcpModelOption`, `claude_acp_session()`, `claude_acp_model_options()` (both with `require_connected`, so an idle-evicted session's remembered list is reachable without claiming it is selectable), `claude_acp_select_model()`; `_record_model_identity()` is now the single writer of the attribute slice A's display accessors read, shared by turn capture and by a pick |
| Shared `/model` seam | `hermes_cli/claude_acp_model_select.py` | `handle_claude_acp_model_command()` — the one owner of the listing, the name resolution (exact id / label / adapter name / unambiguous prefix), the flag policy and every line of copy. Mirrors `hermes_cli/codex_runtime_switch.py`'s one-module-per-shared-command precedent |
| CLI wiring | `cli.py` (`_handle_model_switch`) | Calls the seam right after arg parsing and the `--refresh` cache bust — the same order the gateway uses, so `--refresh` (a fact about Hermes' own on-disk provider catalog, not about this session's list) means the same thing on both surfaces whether or not the seam then answers; renders `text` with the CLI's `✓`/`✗` |
| Gateway wiring | `gateway/slash_commands.py` (`_handle_model_command`) | Same call after the session key is resolved, via `asyncio.to_thread` (the pick is a blocking JSON-RPC round trip and must not run on the event loop), rendering failures through the existing `gateway.model.error_prefix` copy |
| Resident-agent lookup | `gateway/slash_commands.py` | `_resident_agent_for_session()` — `_running_agents` then `_agent_cache`, the same two places `/usage` looks; never creates an agent |

**Acceptance checks.**
1. The offered list is exactly what the live session advertised, in its order,
   with concise labels, the adapter's display names, and exactly one entry
   marked current.
2. A session that advertises no models (older adapter, empty allowlist) offers
   nothing and leaves `/model` untouched — while slice A's identity display
   keeps working. This holds *per connection*: a rebind whose response carries
   no model entry, or one advertising nothing, drops what the previous adapter
   said rather than inheriting it, so `/model` never offers or validates
   against a list the live session did not give.
3. A pick goes out as `session/set_config_option` with `configId: "model"` on
   the live ACP session id, and the adapter records exactly one such call.
4. After a pick: same adapter pid, same ACP session id, still connected, and
   the next turn recalls the previous one (no reseed) — Packet 2's reuse and
   Claude Code's own prompt cache intact.
5. `claude_acp_display_model()` and `claude_acp_model_identity()` reflect the
   pick immediately, with no further turn, and the next turn's usage row is
   priced under the new authoritative id.
6. `agent.model` is unchanged by a pick, no session model override is written,
   no cached agent is evicted, and nothing is persisted to `config.yaml`.
7. Every refusal — no session, an idle-evicted adapter, an unadvertised name, an
   adapter rejection, a turn in flight, **and a session that changed while the
   adapter was answering** — leaves the running model and the displayed identity
   as they were, and an unadvertised name never reaches the wire.
8. `--global` / `--once` are refused by name without switching; `--provider`,
   another provider's model, a non-Claude-Code agent and a seam fault all fall
   through to the unchanged cross-provider path.
9. On the gateway the pick runs off the event loop, and the resident agent is
   found whether it is mid-turn (`_running_agents`) or between turns
   (`_agent_cache`).
10. Existing Packet 0–4A suites plus every `/model` suite on both surfaces stay
    green. As run: 368 tests across the Claude Code ACP files (this slice's own
    included), 30 across the gateway `/model` files, 591 across the
    CLI/TUI-gateway and `hermes_cli` `/model` files, and `tests/cli/` (957),
    `tests/gateway/`, `tests/hermes_cli/`, `tests/agent/transports/` and
    `tests/acp/` in full. The one failure anywhere in that sweep —
    `tests/hermes_cli/test_doctor.py::test_doctor_reports_vercel_backend_diagnostics`
    — reproduces unchanged on a clean `HEAD` worktree and is unrelated
    (Vercel-sandbox doctor diagnostics).

**Safety invariants.** `agent.model` is still never mutated by this runtime,
for exactly slice A's reason. Prompt caching, role alternation, session
continuity, the negotiated permission mode and workspace roots, credential
redaction and authorization are all untouched: a pick sends one control RPC on
an already-negotiated session and re-runs none of the negotiation. Approval
isolation is unchanged — this path never touches `tools/approval.py`, and the
gateway's existing mid-turn `/model` refusal ("wait or /stop") still applies
before the seam is ever reached, with `set_model`'s own `turn_in_flight` guard
as the in-process backstop for surfaces that allow it. That backstop is now
re-established *after* the RPC as well as before it: the round trip runs
unlocked (holding `_lock` across it would block `request_cancel`, i.e. swallow
the user's `/stop`), so the pre-RPC guards are check-then-act and only the
commit can be made safe — a reply that comes back to a retired adapter, a
different ACP session, or a turn that started meanwhile is discarded and
refused. Unreachable from today's surfaces, and fixed anyway: `turn_in_flight`
is a guarantee the next surface to wire `/model` will trust. Every other provider is
byte-for-byte unaffected: the seam returns `None` unless the agent's `api_mode`
is `claude_code_acp` *and* a live session advertises models.

**Stop condition.** Met — no blocker. `session/set_config_option` is a stable,
implemented verb on the pinned adapter, and the advertised option list gave the
gating and allowlist answers the earlier `session/set_model` sketch could not.

---

## Cross-cutting invariants (all packets)

- **Prompt caching is sacred.** Never rebuild the system prompt or replay past
  context mid-conversation. Claude Code owns its own history; Hermes sends the
  new turn only.
- **Strict role alternation.** No two same-role messages in a row; no synthetic
  user message injected mid-loop.
- **Credential scrubbing.** No token, key, or credential value is printed or
  projected into a *display* field — every projected display/tool/permission
  field is scrubbed before truncation. Packet 0 held this line and later packets
  must too. Two deliberate, named exceptions exist and are scoped in
  `claude_acp_event_projector`'s module docstring: assistant reply text is
  accumulated raw (the response must be byte-identical to what the model said;
  the streaming surfaces scrub downstream), and
  `PermissionRequest.declared_command` / `.declared_paths` are raw, never
  rendered, and exist only so the unconditional approval floor measures what the
  agent declared rather than a redacted rendering of it — the floor logs a
  truncated declared value at WARNING when it blocks, exactly as
  `check_all_command_guards` does for Hermes' own shell.
- **Workspace containment.** Enforced through session roots and permission
  mode.
- **Deterministic teardown.** Every exit path reaps the adapter and releases
  the lease.
- **Dependency policy.** Zero new Python dependencies so far. The Node adapter
  is exact-pinned in one constant and installed through the existing managed
  Node path, not a raw env var.
- **Testing.** `scripts/run_tests.sh` only. CI coverage uses the fake ACP agent
  (no network, no quota); the live probe is run manually on version bumps.
