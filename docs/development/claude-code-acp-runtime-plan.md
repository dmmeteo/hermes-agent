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
Packet 5 (dogfooding tuning) **complete — slices A (profile-local permission
mode), B (gateway runtime metadata), D (persistence projection) and C
(profile-safe skill bridge)**. The permission posture is now
`model.claude_code_acp.permission_mode`, per profile, `default` unless opted out;
the messaging gateway's runtime footer reports the live Claude Code model and the
adapter's own context occupancy instead of `claude-code · 0%`; a tool-using turn
leaves canonical `(assistant tool_calls, tool)` rows in `state.db`; and
`model.claude_code_acp.skills` can put a bounded, manifest-owned, regenerated set
of Hermes skills into the adapter's own `<config dir>/skills` — **off by
default**. Slice C's one open wire question (skill-discovery timing) is settled
against the pinned adapter's code *and* a live probe: discovery happens per
session creation, so the links must exist before `session/new`. See the Packet 5
section.

**Transcript-level tool rows (3A's R2) are now closed** by Packet 5 slice D:
`agent/claude_code_runtime.py` splices the projector's
`(assistant tool_calls, tool)` pairs into `messages` before the assistant text,
the way `agent/codex_runtime.py` always has, so `agent/curator.py`,
external-memory review, background-review snapshots and `session_search` see what
a Claude Code turn *did* and not only what it said. No backfill: a session that
ran before this lands keeps its text-only history and gains rows from its next
turn on. Two items the Packet 3 review left open are still not implemented: a
Hermes-side post-cancel grace deadline (3C R3 / review L1) and any adapter-side
pre-approved-tool policy (3B R1 / 3C R1 / review M3).
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
| Permission posture | new `agent/claude_acp_permissions.py` | **Exists (Packet 5A)** — turns `model.claude_code_acp.permission_mode` into the mode negotiated against what the live session advertises, or into sending nothing at all (`inherit`) |
| Skill bridge | new `agent/claude_acp_skills.py` | **Exists (Packet 5C)** — plans and materializes a bounded, manifest-owned link farm in the **adapter child's** `<config dir>/skills`, from `model.claude_code_acp.skills`. Off by default; the link dir is derived from the same `hermes_subprocess_env` the client spawns with, never `Path.home()` |
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
5. A test asserts the negotiated permission mode is the one **Hermes' config**
   requested — `default` unless the profile explicitly opted out (Packet 5
   slice A). A silent `auto` default is a security regression; a written-down,
   per-profile, WARNING-logged `auto` is a decision its owner made. The
   invariant the check exists for survives verbatim: with no config key the
   negotiated mode is still `default`, and nothing the adapter advertises can
   produce a self-approving mode on its own.
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
| Gateway displays | `gateway/slash_commands.py` | `_display_model_for()` / `_metadata_model_for()` — the same preference on `/usage`, `/context` and `/agents`; `/context` sizes its window from the accounting id, not the label. Added with slice B, which is what gave the gateway a `/model` to disagree with. Both bodies now live in `gateway/model_identity.py` (Packet 5 slice B) so the gateway runtime footer reads the same two functions |
| Gateway runtime footer | `gateway/run.py` (`_turn_runtime_metadata`) | The one surface this table was missing: `TurnRunner.run_sync` read `agent.model` directly, so the footer said `claude-code` while `/usage` in the same session said `sonnet-5`. Closed by Packet 5 slice B |
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

## Packet 5 — dogfooding tuning

**Source.** Real dogfooding feedback on a live profile, mapped to four
independent slices (A: profile-local permission mode; B: gateway runtime
metadata — the footer's `claude-code · 0%`; C: profile-safe skill bridge;
D: persistence projection, which closes 3A's R2). Recommended order
A → B → D → C; A and B touch none of each other's files. All four have landed in
that order. Packet 5 is complete.

### Slice A — profile-local permission mode ✅ COMPLETE

**Goal.** The permission posture of a Claude Code session is a config decision
the profile owner makes and can inspect, with the current behaviour as the
default, and with the option of *not deciding* (letting Claude Code's own
`settings.json` stand).

**The bug it fixes.** `session/set_mode` → `default` ran on every connect from a
module constant with no config path, so a profile's own
`~/.claude/settings.json` `permissions.defaultMode` was honoured by the adapter
at session creation and then immediately overwritten by Hermes. `model:` in the
same file *did* take effect, because Packet 4A only ever reads the model and
never writes it at session start. That asymmetry was the whole report, and it
was one line of policy.

**Config schema.** One new key, no `.env` mirror, no `HERMES_*` var, no
`DEFAULT_CONFIG` entry and no `_config_version` bump — consistent with the two
`claude_code_acp` keys that already live outside the defaults (`command`,
`additional_directories`), and with AGENTS.md §Adding Configuration:

```yaml
model:
  claude_code_acp:
    permission_mode: default   # default | plan | acceptEdits | auto |
                               # dontAsk | bypassPermissions | inherit
```

`default` (the default) and `plan` keep every permission request coming to
Hermes. `acceptEdits` / `auto` / `dontAsk` / `bypassPermissions` are
self-approving: the adapter decides most tool use itself, so Hermes' approval
gate — and with it the unconditional floor (hardline blocklist, sudo-stdin
guard, `approvals.deny`) — only measures what it is still asked about. `inherit`
sends **no** `session/set_mode` at all.

**Migration / default behaviour.** Absent key, unreadable config, non-string
value, unknown value, a mode the live session does not advertise, and a session
that reports no mode state at all **all** resolve to `default` — byte-identical
to pre-Packet-5 behaviour. Every rejection narrows, and each logs one WARNING
naming the value and the fallback. `inherit` is reachable only by naming it: no
failure mode produces "Hermes negotiated nothing" by accident.

**Seams landed.**

| Seam | File | What it does |
|---|---|---|
| Config + policy | new `agent/claude_acp_permissions.py` | `CONFIG_KEYS`, `DEFAULT_PERMISSION_MODE`, `HERMES_GATED_MODES`, `SELF_APPROVING_MODES`, `INHERIT`, `KNOWN_MODES`; `configured_permission_mode()` (never raises, never validates); `resolve_permission_mode(configured, advertised)` — the whole policy in one pure function, `None` ⇒ send no `set_mode`; `describe_posture()` for the audit line |
| Wire capture | `agent/transports/claude_acp.py` | `session_new()` / `session_load()` stash the response's `modes` block; `available_mode_ids` (`None` = no block at all, `[]` = advertises none — the same two-fact discipline as `available_model_options`) and `current_mode_id` read it back; `set_mode()` re-captures a `modes` block if the agent returns one |
| Wire-shape extractors | `agent/transports/claude_acp_event_projector.py` | `extract_current_mode_id()` / `extract_available_mode_ids()` — siblings of Packet 4's model extractors. Unlike the model, the mode has a first-class ACP field and its own notification, so these read `modes`, not `configOptions` |
| Mode-change projection | `agent/transports/claude_acp_event_projector.py` | `current_mode_update` → `ProjectedEvent(kind="mode_changed", mode_id=…)`, deduped within a turn exactly like `config_option_update`. State capture only: **no** display surface, no chat bubble, and deliberately no re-negotiation (answering a user's own live change with another `set_mode` would fight them, mid-turn, on the turn thread) |
| Negotiation | `agent/claude_acp_session.py` | `PERMISSION_MODE` → `DEFAULT_PERMISSION_MODE`; ctor takes `permission_mode`; `_capture_mode_state()` + `_negotiate_permission_mode()` in `_connect()` resolve against **this** session's advertised list and skip `set_mode` entirely for `inherit`. An adapter that refuses the mode aborts the connect rather than prompting anyway |
| Introspection | `agent/claude_acp_session.py` | `negotiated_permission_mode` (what Hermes sent; `None` for `inherit` or no connect) and `effective_permission_mode` (what the wire says is in force — `modes.currentModeId`, the negotiated mode, or a later `current_mode_update`). `AcpTurnOutcome.permission_mode` carries it out per turn |
| Session identity | `agent/claude_acp_session.py` | `matches()` gains the mode: the live adapter has already been put into a posture, so a config change must rebuild rather than silently defer until the adapter is replaced (same argument as `additional_directories`) |
| Runtime wiring | `agent/claude_code_runtime.py` | `_get_or_create_session()` takes the mode and passes `configured_permission_mode()` alongside `resolve_additional_directories(cwd)` |
| Audit log | `agent/claude_acp_session.py` `_connect()` | Exactly one WARNING per connect when the negotiated mode is self-approving, or when `inherit` leaves the adapter's own mode in force — naming the mode and the fact that Hermes' gate will see few or no requests |

**Deliberately unchanged.** The permission bridge
(`make_claude_acp_permission_bridge`),
`tools/approval.py::request_delegated_runtime_approval`, and the unconditional
floor. Any request Hermes *does* receive, in any mode, goes through the same
gate with the same deny-by-default. The mode changes how many requests exist,
never what happens to one — asserted, parametrised over the mode, rather than
argued.

**Acceptance checks.**
1. Default config ⇒ `set_mode_calls == ["default"]` on a new session **and** on
   a rebind; the three pre-existing assertions in
   `test_claude_code_runtime_safety.py`, `test_claude_code_permission_bridge.py`
   and `test_claude_code_session_continuity.py` stay green untouched.
2. `permission_mode: auto` ⇒ `["auto"]`, and the turn completes.
3. `permission_mode: inherit` ⇒ zero `set_mode` calls, and
   `effective_permission_mode` reports what `session/new` said `currentModeId`
   was.
4. A mode the live session does not advertise ⇒ `default` negotiated + one
   WARNING; nothing unadvertised reaches the wire.
5. An adapter that reports no `modes` at all ⇒ `default` negotiated, never
   `inherit` by accident.
6. A self-approving mode still routes any request that does arrive through
   `request_delegated_runtime_approval`; a denial still blocks the write, and
   the sudo-stdin floor still refuses without consulting a human.
7. `matches()` is False when only `permission_mode` changed ⇒ the next turn
   rebuilds the session.
8. A mid-turn `current_mode_update` updates `effective_permission_mode` and
   fires no display callback.
9. All existing Claude ACP suites green — 604 tests across
   `tests/agent/test_claude_code_*.py` and `tests/agent/transports/`, plus the
   CLI/gateway/`hermes_cli`/TUI ACP files.

**Test seams.** `agent/claude_acp_permissions.{configured_permission_mode,
resolve_permission_mode}`; `ClaudeAcpSession(permission_mode=…)` /
`.negotiated_permission_mode` / `.effective_permission_mode` / `.matches(argv,
cwd, dirs, permission_mode)`; `ClaudeAcpClient.available_mode_ids` /
`.current_mode_id` / `.session_modes`; the projector's `mode_changed`. The fake
adapter gained `FAKE_ACP_MODES`, `FAKE_ACP_NO_MODES`, `FAKE_ACP_CURRENT_MODE`,
`FAKE_ACP_REJECT_SET_MODE`, `FAKE_ACP_SWITCH_MODE` and a `mode-switch` turn
mode, and now answers `session/new` / `session/load` with a real `modes` block;
the Packet 0 fake agent (`tests/agent/transports/_fake_acp_agent.py`) answers
with one too and validates `session/set_mode` against it. New file
`tests/agent/test_claude_code_permission_mode.py` (44 tests), plus
`TestPermissionModeState` in `tests/agent/transports/test_claude_acp.py` and
`TestExtractModeState` in the projector's test file.

**Safety invariants.** The unconditional approval floor, prompt caching, role
alternation, workspace roots, credential scrubbing and `agent.model` are all
untouched. The one guarantee this slice genuinely lets a profile owner give up
is Hermes' visibility into the requests a self-approving adapter answers itself
— which is why it is an enumerated opt-in, per profile, logged once per connect,
and documented in the key's own comment.

**Open question carried forward (not implemented here).** Should a
self-approving mode be refused when the turn is non-interactive and
`approvals.cron_mode: deny`? That is the one place this design lets config
remove a guarantee `tools/approval.py` currently makes unconditionally.
Recommendation: refuse it there, log once, negotiate `default` — opting into
`auto` for your own chat is a different decision from opting into it for
unattended cron. Left for a follow-up so this slice stays the one policy
function it is.

### Slice B — gateway runtime metadata ✅ COMPLETE

**Goal.** Every gateway metadata consumer sees the model Claude Code is
actually running and the context occupancy Claude Code actually reported,
through the same seam the other four surfaces already use.

**The bug it fixes.** The reported footer was `claude-code · 0% · ~/projects`
for a session running Opus a third of the way through its window. Both halves
came from four lines in `TurnRunner.run_sync` that read the **agent object**
where the CLI status bar, `/usage`, `/context` and the TUI's `session.info`
read the **runtime seam**: `agent.model` is the static pseudo-model Packet 4A
deliberately never mutates (the gateway's agent cache evicts on a changed
`model`, so writing it would rebuild the agent and destroy prompt caching), and
Hermes' compressor is deliberately not driven on this runtime (Packet 3A), so
`last_prompt_tokens` was 0 against a non-zero window — and `round(0/N*100)`
renders as a real-looking `0%`. The runtime was already returning better
numbers (`context_used_tokens` / `context_window_tokens` from `usage_update`)
and `run_sync` overwrote them.

**Config schema.** None. No new keys; `display.runtime_footer.{enabled,fields}`
already exists. No migration: for every non-ACP agent the new function returns
field-for-field what the four lines returned.

**Seams landed.**

| Seam | File | What it does |
|---|---|---|
| Shared display seam | new `gateway/model_identity.py` | `display_model_for(agent)` / `metadata_model_for(agent)` — the two staticmethods from `gateway/slash_commands.py` moved out verbatim, so the footer can reach them without importing the turn runner. The staticmethods stay as one-line delegations: `/usage`, `/context` and `/agents` cannot drift from the footer because there is one implementation |
| Testable metadata function | `gateway/run.py` `_turn_runtime_metadata(agent, result)` | Module-level, no event loop, no adapter, no `TurnContext` — which is what makes the slice testable at all (`run_sync` is ~1300 lines). Returns `{model, model_id, context_length, last_prompt_tokens, input_tokens, output_tokens}`; `run_sync` calls it once and spreads it into both result dicts |
| Resolution order | same | `model`: display label → `agent.model`. `context_length`: adapter-reported `context_window_tokens` → compressor → 0. `last_prompt_tokens`: adapter-reported `context_used_tokens` (the number Claude Code's own `/context` shows) → the compressor's reading, **including its `-1` "unknown" sentinel** → the turn's own reported prompt tokens → 0. A reported value that is absent, the wrong type, or negative falls through rather than becoming a number on someone's footer |
| Id path kept separate | `gateway/run.py` | New `model_id` key carries `metadata_model_for()` — an id, never a label — and the `agent:end` hook payload reads it, so hook handlers keep correlating on the same shape they always did |
| Occupancy kept out of the compression heuristic | `gateway/run.py` | New `transcript_prompt_tokens` key carries the compressor's reading alone, and the session entry stores *that*. Session hygiene sizes this session's own history with the stored value before deciding to auto-compress it, and a delegated runtime's window occupancy — which counts tool traffic Hermes never stored — is not a measurement of that history. Identical to `last_prompt_tokens` on every runtime that drives the compressor, so nothing else moves |
| Occupancy survives a quiet turn | `agent/claude_acp_session.py` | `_last_context_usage`: `usage_update` arrives on a per-turn projector, but how full the window is, is a standing fact about the *session*. `AcpTurnOutcome.context_usage` falls back to it, so the footer no longer flickers between a real percentage and none. Dropped in `_drop_client` with the adapter that reported it, and **carried forward with its cost stripped** — occupancy is a standing fact, a cost is an event, and re-reporting one would invent spend that never happened |

**Deliberately unchanged.** `agent.model` (Packet 4A's finding: the agent cache
evicts on it), Hermes' compressor (Packet 3A's decision),
`gateway/runtime_footer.py` (already correct), and
`conversation.last_resolved_model` — audited and confirmed to be fed from
config resolution, never from this dict, so no display label can reach the
model a recovery turn rebuilds an agent with.

**Acceptance checks.**
1. `_turn_runtime_metadata` returns the display label for an ACP agent with a
   captured identity, and `agent.model` for every other agent.
2. Occupancy comes from the adapter when reported, the compressor otherwise;
   a turn that reported real occupancy no longer renders `0%`.
3. `agent.model` is bit-for-bit unchanged, so no cached agent is evicted by the
   post-turn `_agent.model != _cfg_model` check.
4. Footer, `/usage`, `/context` and `/agents` report the same label for the same
   agent — asserted as function identity, not as three matching strings.
5. A turn that emits no `usage_update` reuses the session's last snapshot
   rather than dropping to `0%`, and reports no cost for it.
6. The Codex app-server runtime (which drives the compressor *and* returns
   `last_prompt_tokens`) is unaffected, and the post-compaction `-1` sentinel
   still suppresses the field.
7. `tests/gateway/` (5161), `tests/cli/` (959), `tests/test_tui_gateway_server.py`
   and the ACP suites green.

**Test seams.** `gateway.model_identity.{display_model_for,metadata_model_for}`;
`gateway.run._turn_runtime_metadata(agent, result)`;
`gateway.runtime_footer.build_footer_line(...)` for the end-to-end string. New
file `tests/gateway/test_runtime_footer_claude_acp.py` (15 tests, including the
red-capable `opus-5 · 37% · ~/projects` — a behaviour under fixed inputs, not a
catalog snapshot), the shared-seam assertion in
`tests/gateway/test_claude_acp_model_display.py`, and two occupancy-lifetime
tests in `tests/agent/test_claude_code_event_projection.py` driven through a new
`usage-once` fake-adapter mode (occupancy reported on the first turn of a
session and never again).

### Slice D — persistence projection ✅ COMPLETE

**Goal.** A Claude Code turn leaves the same shape in `state.db` as a Codex turn,
so `session_search`, `agent/curator.py`, external-memory review and
background-review snapshots see the tool activity — with no duplicated bubble, no
broken alternation, no orphan `tool_calls`, and no cache effect. Closes 3A's R2.

**The gap it fixes.** The turn appended exactly one message
(`{"role": "assistant", "content": final_text}`) and flushed it. Measured on the
reporter's live DB: 6 `role='tool'` rows from the native agent, **0** across ~16
Bash calls under Claude Code, and `sessions.tool_call_count` frozen at 6.
`hermes_state.py`'s counter is driven by `num_tool_calls` at append time, so it
was right — there was nothing to count.

**Config schema.** One new key, no `.env` mirror, no `HERMES_*` var, no
`DEFAULT_CONFIG` entry and no `_config_version` bump — same treatment as the
three `claude_code_acp` keys already outside the defaults:

```yaml
model:
  claude_code_acp:
    persist_tool_calls: true   # default TRUE — a kill switch, not a feature gate
```

It governs **both** state.db writes this slice makes (the transcript pairs and
the activity stamp), because they are one decision: "keep a Hermes-side record of
what the adapter did". Default on, because parity with the sibling delegated
runtime is what the plan asked for, codex has no equivalent flag, and there is no
user-visible surface to regress. It exists at all because the rows land in a live
session DB a heavy user has already grown to hundreds of megabytes. Every
rejection (absent key, unreadable config, non-flag value) resolves to `true` and
a non-flag value logs one WARNING.

**Seams landed.**

| Seam | File | What it does |
|---|---|---|
| Transcript rows | `agent/transports/claude_acp_event_projector.py` | `projected_messages` — one `(assistant tool_calls, tool)` pair per completed tool call, in completion order, built by `_transcript_pair()`. `PERSISTED_RESULT_MAX_CHARS` (4000) + a counted `… [+N chars truncated]` marker; `UNREPORTED_TOOL_RESULT` for the orphan repair; `[failed]` marks a failed call, since a row has only its text where the display surface has structured `is_error` |
| Bound split | same | `_scrubbed_result_text()` (scrubbed, unbounded) now feeds both the display cut and the transcript cut. Scrub once, before either — cutting first can split a secret into a prefix the redactor no longer recognises — and the row is **not** derived from the display string, whose bare ellipsis carries no count |
| Orphan repair | same | Computed on *read*, not flushed by a caller: a tool with no terminal update is closed inside `projected_messages`, so **every** exit path (success, non-`end_turn` stop, cancel mid-tool) yields a list with no orphan `tool_calls` row, reading twice gives the same list, and there is no "someone forgot to call flush" failure mode |
| Turn carrier | `agent/claude_acp_session.py` | `AcpTurnOutcome.projected_messages`, read once after the prompt settles |
| The splice | `agent/claude_code_runtime.py` | `messages.extend(outcome.projected_messages)` → assistant text → **one** `_persist`. Order: `user` → `[assistant(tool_calls), tool]*` → `assistant(text)` |
| Kill switch | `agent/claude_code_runtime.py` | `PERSIST_TOOL_CALLS_CONFIG_KEYS`, `claude_acp_persist_tool_calls()` — read once per turn, not per event |
| Activity clock | `agent/claude_code_runtime.py` | `make_claude_acp_event_bridge(agent, observe_activity=…)` calls `_touch_activity(f"claude code: {tool.name}")` on each completed tool (adjacent finding 0.5.1). Mid-turn deliberately: a stamp written at turn end says nothing about liveness. `last_activity_description` is still cleared when the turn ends by `_reset_activity_labels_after_turn` — that is Hermes' own design (#72039), so the durable gain here is `last_activity_at` |
| Fake adapter | `tests/agent/_fake_claude_acp_adapter.py` | `FAKE_ACP_HUGE_TOOL_OUTPUT` (oversized result) and a `cancel-mid-tool` mode — a `tool_call` that never reports an outcome, the one shape that can produce an orphan |

**Why the cache and alternation are safe.** This runtime never replays `messages`
into the adapter (Hermes sends the new turn only), so the rows are Hermes-side
records and the adapter's prompt cache is untouched. The one thing that ever goes
back is the reseed recap, and `_build_recap` filters to string-content
user/assistant rows — which excludes both the tool rows and the `content=None`
assistant rows carrying `tool_calls`. Asserted byte-identical, not argued.
Alternation holds because the pairs sit between the user row and the reply.

**Why there is no duplicate bubble.** Display and persistence are two disjoint
consumers of one projector: the event bridge fires the callbacks and appends no
message; `projected_messages` is read once at turn end and fires no callback.
Asserted as *equal callback counts with the switch on and off*, not argued. A
`session/load` replay cannot manufacture rows either — the projector is
constructed after `_connect()` and after `drain_inbox()`, and only
`client.prompt`'s `on_update` feeds it.

**Also verified rather than assumed.** New `role:"tool"` rows cannot start
auto-delivering attachments: `gateway/run.py` gates MEDIA extraction on
`_AUTO_APPEND_MEDIA_TOOL_NAMES` (four Hermes tool names), and `Bash`/`Write`/
`Read` are not among them. `agent_persisted` stays `True`, so the gateway still
writes nothing — flipping it would re-INSERT the already-flushed user turn
(#860/#42039).

**Acceptance checks.**
1. A tool-using turn writes one `(assistant tool_calls, tool)` pair per completed
   tool; `role='tool'` count == completion count == 4; `tool_call_count` advances
   to 4; ids are `deterministic_call_id(<adapter toolCallId>)`.
2. Neither planted credential appears anywhere in any persisted row, and the
   secret argument is masked rather than dropped.
3. No orphan `tool_calls` row on any exit path — including a stop mid-tool, which
   lands a repaired pair carrying `UNREPORTED_TOOL_RESULT`.
4. Exactly one user row and exactly one final assistant text row;
   `agent_persisted` stays `True`.
5. Display callback counts are identical with recording on and off.
6. `session_search` finds a tool result by its text.
7. `_build_recap` is byte-identical with and without tool rows.
8. `persist_tool_calls: false` reproduces the pre-slice transcript exactly —
   `["user", "assistant"]`, `tool_call_count == 0`, `last_activity_at` NULL.
9. `last_activity_at` is non-NULL after a tool-using turn.
10. An oversized result is truncated with a marker naming how much was dropped;
    an ordinary result is untouched.
11. All Claude ACP suites green — 563 tests across the 21 test files that
    touch this runtime (`grep -rln 'claude_acp\|claude_code_acp\|_fake_claude_acp'
    tests/`, minus the two files where the runtime is incidental,
    `test_cli_status_bar.py` and `test_tui_gateway_server.py`) — plus
    curator/state (407), `session_search` + codex-persist parity (68), and every
    suite that touches `_flush_messages_to_session_db` / `agent_persisted` (1190
    across 39 files). The earlier "477 across 16 files" here counted only the
    `agent/` + `transports/` + two gateway files and read as though it were the
    whole runtime; the criterion is now stated so the number can be reproduced.

**Test seams.** `ClaudeAcpEventProjector.projected_messages`,
`PERSISTED_RESULT_MAX_CHARS`, `UNREPORTED_TOOL_RESULT`, `deterministic_call_id`;
`AcpTurnOutcome.projected_messages`;
`claude_code_runtime.claude_acp_persist_tool_calls()`;
`make_claude_acp_event_bridge(agent, observe_activity=…)`. New file
`tests/agent/test_claude_code_transcript_projection.py` (39 tests, real
fake-adapter subprocess + real temp `SessionDB` — the failure was found by
querying `state.db`, so that is what the tests query), plus
`TestTranscriptProjection` (28 pure-data tests) in the projector's test file.
`test_role_alternation_holds` in `test_claude_code_event_projection.py` was
updated: it froze the pre-slice row list, and now asserts the invariant (no
adjacent same-role rows, ends on the assistant) plus the new canonical order.

**Open question carried forward.** Keep `persist_tool_calls` at all? Codex has no
equivalent. Recommendation unchanged: keep it for one release as a dogfooder
retreat, then delete it.

### Slice C — profile-safe skill bridge ✅ COMPLETE

**Goal.** A configured, bounded, inspectable set of Hermes skills becomes
discoverable to Claude Code inside the profile, with no always-loaded prompt
bloat, no global-symlink breakage, and no writes anywhere Hermes does not own.

**The gap it fixes.** `<adapter HOME>/.claude/skills` did not exist, so the
adapter's session saw only Claude-native and plugin skills and nothing Hermes
owns. Hermes' layout is `<root>/<category>/<skill>/SKILL.md` — two levels where
Claude Code's personal-skill discovery is one — across three roots holding 256
skills with 20+ duplicate basenames, so the bridge has to flatten and a flat farm
cannot be collision-free by construction. "Link everything" was never an option.

**The wire question, settled.** The map recorded discovery timing as
**(inferred)**. It is now measured, twice:

- *Adapter code.* `createSession` in
  `@agentclientprotocol/claude-agent-acp@0.66.0` (`dist/acp-agent.js:4562`)
  builds the SDK `query()` and `await`s `q.initializationResult()`, and both
  `session/new` and `session/load` route through it. The SDK resolves its config
  dir as `process.env.CLAUDE_CONFIG_DIR ?? homedir()/.claude`
  (`@anthropic-ai/claude-agent-sdk@0.3.220`, `sdk.mjs`), and the `~/.claude/skills`
  auto-load is the `@skills-dir` plugin scan the settings schema names. The SDK
  exposes a `reloadSkills()` control request; **the adapter never calls it**.
- *Live probe* (`initialize` + two `session/new`, no prompt, temp `HOME`, so no
  model quota): a skill present before `session/new` **is** discovered; one added
  afterwards is **not**, and no `commands_changed` / `available_commands_update`
  arrives; a second `session/new` on the **same adapter process** does pick it
  up.

So discovery is **per session creation** — not per process, not per prompt. The
timing constraint the map inferred holds exactly, and nothing else in the design
changed. The consequence for `matches()` is now a measurement rather than a
preference: a live adapter genuinely cannot be re-scoped, so a skills-config
change takes effect at the next connect.

**Config schema.** One new key tree, no `.env` mirror, no `HERMES_*` var, no
`DEFAULT_CONFIG` entry and no `_config_version` bump — same treatment as the four
`claude_code_acp` keys already outside the defaults:

```yaml
model:
  claude_code_acp:
    skills:
      bridge: off          # off | link      (DEFAULT: off — zero fs writes)
      roots: []            # extra skill roots; [] = Hermes' own resolved roots
                           #   (get_skills_dir() + skills.external_dirs)
      include: []          # REQUIRED for `link`. Selectors, config order:
                           #   "software-development/code-review"  exact
                           #   "code-review"                       bare name
                           #   "software-development/*"            one category
                           # No "**", no regex, no bare "*".
      exclude: []          # same grammar, applied after include
      max_links: 40        # clamped to 200; overflow is dropped AND named
      prune_unmanaged: false   # never the recommendation; see below
      link_dir: ""         # override; default <adapter config dir>/skills
```

`bridge: off` by default ⇒ no directory, no manifest, no writes, and every
pre-existing suite untouched. `bridge: link` with an empty `include` links
**nothing** and logs one WARNING: an explicit allow-list, never an implicit
"everything".

**Seams landed.**

| Seam | File | What it does |
|---|---|---|
| Config | **new** `agent/claude_acp_skills.py` | `CONFIG_KEYS`, `BridgeConfig`, `resolve_bridge_config()`, `parse_selectors()`. Every rejection narrows to `off` / fewer links |
| Where | same | `child_spawn_env()` + `default_link_dir()` — built from the **same** `hermes_subprocess_env` → `apply_subprocess_home_env` chain `ClaudeAcpClient` spawns with, honouring `CLAUDE_CONFIG_DIR` then `HOME`/`USERPROFILE`. One source of truth, so the dir Hermes writes and the dir the child reads cannot drift. Never `Path.home()` (AGENTS.md §Profiles rule 1) |
| Plan | same | `discover_skills()` (depth ≤ 2, readable `SKILL.md` required, `skills.disabled` honoured), `plan_skill_links()` → `SkillLinkPlan(links, dropped_over_cap, unmatched_selectors, refused_outside_roots, tool_vocabulary_notes)`. Pure: reads, never writes |
| Naming | same | `<skill>` when unique in the plan; else `<category>-<skill>`; else `<root-slug>-<category>-<skill>`, where a root slug that is not unique carries a digest of the root path. Escalation is recomputed over the whole set each round, so **root order cannot change the answer** |
| Materialize | same | `materialize_skill_links()` + `read_manifest()`. `<link_dir>/.hermes-claude-skills.json` (`version`, `generated_by`, `config_hash`, `link_dir`, `links[]`) written atomically via a temp file + `replace` |
| Hook | `agent/claude_acp_session.py` `_connect()` | one guarded `bridge_skills()` before the client is even spawned — strictly earlier than `session/new`, so an adapter that ever moved the scan to startup would still see the links. Result kept on the session as `last_skill_bridge_result` |
| Fake adapter | `tests/agent/_fake_claude_acp_adapter.py` | `skills_dir_listings` — the child dumps its own `<CLAUDE_CONFIG_DIR or $HOME/.claude>/skills` listing **at `session/new` / `session/load`**, plus the `usable` subset it can actually read through. That is what makes the E2E an assertion about *ordering* rather than about files existing by the end of the turn |

**Manifest ownership is the whole safety story.** Only names the manifest claims
are ever replaced or removed. An entry it does not claim — a real directory, a
hand-made symlink, anything — is left completely alone and reported as
`skipped_unmanaged`, and it is not adopted into the manifest either. A missing,
unreadable, malformed or future-versioned manifest owns **nothing**, which is the
narrow reading. A real directory is never replaced even when the manifest *does*
claim it: deleting a human's skill to install ours is the one unrecoverable move
available here. `prune_unmanaged` exists only so the option is explicit; it
defaults false, it removes stray *symlinks* only, and the recommendation is never
to set it.

This matters concretely: the reporter's real `~/.claude/skills` holds **8
hand-made symlinks**, dangling since the 2026-07-27 reorg pointed them at a flat
`core-skills/<name>` layout that no longer exists. On a **non-container** host
install `get_subprocess_home()` returns `None`, so the adapter's `HOME` *is* the
real user home and that is exactly the directory this bridge would write into.
Under this design those 8 links are reported and untouched. (On the reporter's
own host `is_container()` is true, so the link dir is
`{HERMES_HOME}/home/.claude/skills` — measured, not assumed.)

**Self-healing is the other half.** Managed links are regenerated from config on
every connect, so the created-once-and-never-regenerated failure the 8 stale
links represent is structurally impossible for managed entries: a managed link
whose target moved is repointed, and one whose skill vanished is removed (and
counted separately when it was already dangling, `broken_pruned` vs `removed`).

**Fail-open, and never a turn.** `bridge_skills()` cannot raise. A read-only
home, a Windows symlink-privilege refusal, an unreadable config — each costs one
WARNING and a session with no bridge. Per-entry failures are collected rather
than aborting the batch, so one bad link cannot cost the other thirty-nine.
**No copy fallback on Windows**: a stale copy of a skill is worse than no skill,
and it would write into a tree Hermes does not own.

**Containment.** A skill directory that resolves outside every configured root is
refused (`refused_outside_roots`) rather than linked. Skills are meant to be
readable; *which* tree the adapter can read through a Hermes-made link is still a
decision, and this keeps the guarantee statable. Nothing is ever written into a
skill root — `skills.external_dirs` are externally owned, and a test asserts the
root tree is byte-for-byte unchanged across a materialization.

**Tool vocabulary — the feature's real correctness risk.** AGENTS.md §Skill
authoring standards rule 2 requires Hermes SKILL.md prose to name **Hermes**
tools in backticks (`read_file`, `terminal`, `patch`, `search_files`). The Claude
Code session has none of them; it has `Read`, `Bash`, `Edit`, `Grep`. A bridged
skill written in Hermes' vocabulary will instruct the model to call tools that do
not exist in that session. That is a correctness problem, not a safety one, and
it is why `include` is an explicit allow-list with no default.

`agent/claude_acp_skills.py` carries the translation table
(`HERMES_TO_CLAUDE_TOOLS`) and reports — never rewrites — per bridged skill:
`SkillLink.hermes_tool_mentions`, surfaced in `SkillLinkPlan.tool_vocabulary_notes`
and logged once per connect. Detection is **backtick-scoped** deliberately: bare
"terminal" and "recall" are ordinary English and would otherwise flag almost
everything. Rewriting an external skill to fix its vocabulary is out of the
question — external dirs are externally owned, and a Hermes-side edit would be a
silent fork.

Measured on this host, which is why the recommendation below is what it is:

| Root | SKILL.md files | Name a Hermes tool in backticks |
|---|---|---|
| `~/.hermes/core-skills` | 23 | **0** |
| `~/.hermes/profiles/pewds/skills` | 150 | **55** |

**Recommended pewds opt-in (not applied — no live config was edited).** Core
skills only, which is the root whose prose is already tool-agnostic:

```yaml
model:
  claude_code_acp:
    skills:
      bridge: link
      roots:
        - /home/me/.hermes/core-skills
      include:
        - software-development/code-review
        - software-development/diagnosing-bugs
        - software-development/tdd
        - productivity/grilling
        - productivity/writing-for-agents
        - autonomous-ai-agents/handoff
```

Naming `roots` explicitly is what keeps a bare-name selector from reaching into
the 150-skill profile root. Read the two comments together with slice A's
`permission_mode`: a self-approving mode plus a bridged skill that shells out is
the widest combination in this packet.

**Cache and identity.** Nothing here touches the system prompt or replays
context: links cost the adapter roughly one `name` + `description` line each via
its own progressive disclosure, on its side of the wire. `matches()` deliberately
does **not** include the skills config — a changed key takes effect at the next
connect, not by retiring a healthy adapter mid-conversation. Stated as a decision
so it is not read as an omission.

**Acceptance checks.**
1. `bridge: off` (default) ⇒ no directory created, no manifest, the child's
   listing at `session/new` is `[]`, and the 21 pre-existing runtime suites are
   untouched.
2. Configured skills appear as links **before** `session/new`, and the child can
   read `SKILL.md` through them (`usable == entries`).
3. A basename collision across two roots produces two distinct deterministic
   names; a collision that survives the category gets the root slug; neither is
   silently dropped; reordering the roots does not change the names.
4. An unmanaged entry — real directory or hand-made symlink — is never
   overwritten, is reported, and is not adopted into the manifest.
5. A managed link whose target moved is repaired on the next connect; a broken
   one self-heals; one whose skill vanished is pruned and drops out of the
   manifest.
6. `max_links` overflow is dropped, **named**, and logged with the count kept.
7. The link dir resolves under the child's home: two different `HERMES_HOME`s
   with `terminal.home_mode: profile` get two different dirs, and
   `CLAUDE_CONFIG_DIR` wins over `HOME` exactly as the SDK resolves it.
8. A materialization failure (read-only dir) logs a WARNING and the turn still
   completes normally.
9. Prompt cost is bounded: the plan length is ≤ `max_links` (the honest proxy —
   Hermes cannot measure Claude's own context).
10. A skill naming Hermes tools is reported; a tool-agnostic one is not; the
    SKILL.md file is byte-identical after materialization.
11. All Claude ACP suites green (637 across 22 files — the 21 that slice D's
    check names, plus the new one), and 2293 across 64 files for the combined
    ACP + skills + subprocess-HOME/profile sweep. The developer's real
    `~/.claude/skills` is byte-identical afterwards, stale links and all.

**Test seams.** `resolve_bridge_config()`, `parse_selectors()`,
`default_link_dir()`, `child_spawn_env()`, `discover_skills()`,
`plan_skill_links()`, `materialize_skill_links()`, `read_manifest()`,
`bridge_skills()`, `HERMES_TO_CLAUDE_TOOLS`;
`ClaudeAcpSession.last_skill_bridge_result`; the fake adapter's
`skills_dir_listings`. New file `tests/agent/test_claude_code_skill_bridge.py`
(74 tests: 67 pure, 7 driving a REAL fake-adapter subprocess over real stdio).
The ordering tests were confirmed red-capable by stubbing the `_connect()` hook:
5 of the 7 fail with an empty listing, a missing link, or a `None` result — the
other 2 assert absence and correctly stay green.

`HOME` is set per test. `tests/conftest.py` isolates `HERMES_HOME` but
deliberately **not** `HOME`, and this is the one feature in the runtime whose
output path is derived from it — an un-isolated test would write links into the
developer's real `~/.claude/skills`, next to the 8 stale ones.

**Open question carried forward.** `prune_unmanaged` has no known legitimate use:
it exists so the option is explicit rather than implied by the manifest design.
If a release goes by without anyone setting it, delete the key.

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
