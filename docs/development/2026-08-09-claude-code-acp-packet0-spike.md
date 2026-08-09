# Packet 0 spike report — Claude Code over ACP

**Date:** 2026-08-09
**Branch:** `feat/claude-code-acp-runtime`
**Verdict:** ✅ **GATE PASSED.** A real prompt completed over ACP using the
existing Claude subscription (no `ANTHROPIC_API_KEY`), returning typed protocol
events, with a clean process stop.

Companion plan: [`claude-code-acp-runtime-plan.md`](claude-code-acp-runtime-plan.md).

---

## 1. Pinned adapter

The `claude` CLI (v2.1.226) has **no** native ACP mode — `claude --help` lists
no `acp` flag or subcommand. An external adapter is required.

| Property | Value |
|---|---|
| Package | `@agentclientprotocol/claude-agent-acp` |
| Version pinned | **0.66.0** |
| License | **Apache-2.0** (permissive; no GPL/AGPL in the dependency set) |
| Executable | `claude-agent-acp` (`dist/index.js`) |
| Repository | `github.com/agentclientprotocol/claude-agent-acp` |
| Node engine | `>= 22` (host: v22.22.3) |
| Direct deps | `@agentclientprotocol/sdk@1.3.0`, `@anthropic-ai/claude-agent-sdk@0.3.220`, `zod` |
| Install size | 104 packages |

**Naming trap:** the widely-referenced `@zed-industries/claude-code-acp` is
**deprecated** and renamed. Installing it emits:

> `npm warn deprecated @zed-industries/claude-code-acp@0.16.2: This package has
> been renamed to @agentclientprotocol/claude-agent-acp.`

Its last release is 0.16.2 against ACP SDK 0.14.1. Do not pin the Zed name.

## 2. Authentication — subscription works, no API key needed

`initialize` returned **`authMethods: []`** — the adapter reports no auth
challenge because it inherits the already-authenticated Claude Code session.

Verified posture during every live run:

- `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN` — all
  **unset**, and additionally stripped from the child env via `env -u`.
- `~/.claude/.credentials.json` key shape (values never read or printed):
  `{"claudeAiOauth": {accessToken, refreshToken, expiresAt,
  refreshTokenExpiresAt, scopes, subscriptionType, rateLimitTier}}`.

Turns completed and billed against the subscription OAuth credential. **No
credential or token value was read, printed, or logged at any point.**

## 3. Protocol version and capabilities

`PROTOCOL_VERSION = 1` (read from the installed SDK at
`@agentclientprotocol/sdk/dist/schema/index.js`), and the adapter echoed
`protocolVersion: 1`.

```json
{
  "loadSession": true,
  "promptCapabilities": {"image": true, "embeddedContext": true},
  "mcpCapabilities": {"http": true, "sse": true},
  "auth": {"logout": {}},
  "providers": {},
  "sessionCapabilities": {
    "additionalDirectories": {}, "close": {}, "delete": {},
    "fork": {}, "list": {}, "resume": {}
  },
  "_meta": {"claudeCode": {"promptQueueing": true}}
}
```

Session modes offered: `auto` (classifier-driven approvals, **the default**),
`default` (manual prompts), `acceptEdits`, `plan`, `dontAsk`,
`bypassPermissions`.

## 4. Verified capabilities

| Capability | Result | Evidence |
|---|---|---|
| `initialize` | ✅ | protocolVersion 1, capabilities above |
| `session/new` | ✅ | returned a UUID session id |
| `session/prompt` (read-only) | ✅ | `stopReason: end_turn` in 4.8 s; correctly answered `42` from `hello.txt` |
| Typed updates | ✅ | `agent_message_chunk`, `tool_call`, `tool_call_update`, `usage_update`, `session_info_update`, `available_commands_update`, `user_message_chunk`, `config_option_update` |
| Native tools work | ✅ | a real `Read` tool call streamed as `tool_call` → `tool_call_update(completed)` with structured `rawInput`/`rawOutput` — **no leaked JSON/XML in the message text** |
| Cancellation | ✅ | `session/cancel` mid-turn → the prompt resolved with `stopReason: cancelled` |
| Permission bridge | ✅ | in `default` mode a write attempt raised `session/request_permission`; the probe **denied** it and the file was **not** created |
| `session/set_mode` | ✅ | switched `auto` → `default` at runtime |
| `session/load` (same process) | ✅ | replayed history as `session/update` events |
| **`session/load` across adapter restart** | ✅ | **see §5 — the key continuity result** |
| Clean stop | ✅ | closing stdin → exit code **0** on every run |
| `fs/*` client capability | ⚠️ not exercised | Claude Code read the file with its own in-process `Read` tool and never called back into `fs/read_text_file`, even with the capability advertised |

## 5. Session continuity survives an adapter restart

This is the result that de-risks Packet 2. Two **separate adapter processes**:

```
process A: session/new -> e01ae4b8-...  prompt "remember codeword PLUM-SEVEN"
           stopReason=end_turn, "Got it — PLUM-SEVEN."   close -> exit 0
process B: fresh adapter process, session/load(e01ae4b8-...)
           -> OK, replayed {user_message_chunk: 1, agent_message_chunk: 1}
           prompt "what was the codeword?" -> "PLUM-SEVEN"
CONTINUITY: PASS
```

So the ACP session id is a **durable-enough rebinding handle** across process
restarts, not merely an in-process handle. Hermes can keep treating it as a
discardable binding on its own durable conversation id, and the
`session/new` + recap fallback becomes a genuine fallback rather than the
common path.

## 6. Exact commands

```bash
# Adapter metadata + pin
npm view @agentclientprotocol/claude-agent-acp version license bin dependencies engines
npm install --no-audit --no-fund --save-exact \
    @agentclientprotocol/claude-agent-acp@0.66.0

# Unit tests for the retained client (23 tests, real subprocess, no network)
scripts/run_tests.sh tests/agent/transports/test_claude_acp.py
# => 23 tests passed, 0 failed, 5.4s

# Live probe: handshake, read-only turn, permission denial, cancel, load
env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u CLAUDE_CODE_OAUTH_TOKEN \
  HERMES_CLAUDE_ACP_COMMAND=<path>/node_modules/.bin/claude-agent-acp \
  python3 scripts/claude_acp_probe.py --workspace /tmp/acp-probe \
      --probe-permission --probe-cancel
# => PASS: real prompt completed over ACP with typed events and a clean stop
```

Probe output highlights:

```
protocolVersion:   1
authMethods:       []
loadSession:       True
--- turn read-only ---   stopReason: end_turn  elapsed: 4.8s
  updates: {available_commands_update:1, usage_update:6, agent_message_chunk:3,
            tool_call:1, tool_call_update:3, session_info_update:1}
  agent text: "I'll read the file.42"
--- turn write-denied --- stopReason: end_turn  elapsed: 7.8s
  client requests: ['session/request_permission']
  file created despite denial: False
--- turn cancelled ---   stopReason: cancelled  elapsed: 3.8s
session/load OK; replayed: {user_message_chunk:4, agent_message_chunk:4,
                            tool_call:2, tool_call_update:2, ...}
exit code: 0
```

## 7. Retained artifacts

| File | Purpose | Test coverage |
|---|---|---|
| `agent/transports/claude_acp.py` | ACP stdio JSON-RPC client (wire layer only). Stdlib + existing Hermes helpers; **zero new Python dependencies**. | 23 tests |
| `tests/agent/transports/test_claude_acp.py` | Behaviour tests driving a real subprocess over real stdio. | — |
| `tests/agent/transports/_fake_acp_agent.py` | Fake ACP agent used by those tests (not collected by pytest). | — |
| `scripts/claude_acp_probe.py` | Live capability probe; read-only prompts, denies every permission. | run manually |

The client is **not** wired into any Hermes execution path.
`agent/transports/__init__.py::_discover_transports()` uses an explicit import
list, so the module is imported only by its tests and the probe — no footprint
on existing behaviour until Packet 1 wires it deliberately.

## 8. Design findings that shape later packets

1. **Agent-initiated requests arrive mid-turn and MUST be answered.**
   `session/request_permission` and `fs/*` come in *while* `session/prompt` is
   still pending. A client that only waits for the response deadlocks. The
   retained client pumps these on the **caller's** thread, so a slow human
   approval never stalls the reader thread. Two tests cover the "no handler"
   and "handler raised" paths — both must still emit a reply.
2. **Default mode is `auto`**, where a classifier approves/denies without ever
   asking Hermes. For Hermes to own permissions, Packet 3 must set
   `session/set_mode` → `default` explicitly. This is a security-relevant
   default, not a preference.
3. **Tool payloads are already structured** (`tool_call` / `tool_call_update`
   with `rawInput`, `rawOutput`, `locations`, `kind`, `status`). This is the
   direct fix for the leaked-JSON/XML failure of the earlier direct-CLI
   attempt; projection is a mapping job, not a parsing job.
4. **`usage_update` events exist** — token accounting can be projected into
   Hermes' usage tracking rather than estimated.
5. **Prior art to avoid duplicating:** `agent/copilot_acp_client.py` is an ACP
   client that spawns a **short-lived process per request**, flattens the whole
   conversation into a single prompt string, and regex-parses `<tool_call>`
   XML blocks back out of the reply text (`_TOOL_CALL_BLOCK_RE`). It is the
   concrete in-tree example of why `ProviderTransport` alone is too narrow: no
   session continuity, no cancellation, no typed projection, all permissions
   blanket-denied. The delegated-runtime design deliberately does not extend it.

## 9. Unresolved risks

| # | Risk | Impact | Mitigation / next step |
|---|---|---|---|
| R1 | Adapter is pre-1.0 (0.66.0) and was renamed once already; ACP itself is young. | Breaking changes between releases. | Exact pin, assert `protocolVersion == 1` at handshake (already enforced), capability-gate every optional call. |
| R2 | Node **runtime** dependency; adapter is not a Python package. | Install/UX burden; version skew. | Packet 1 must route install through `hermes tools` / auto-install alongside the existing `~/.hermes/node`, not a raw env var. |
| R3 | On stdin close the adapter can emit `Unhandled Rejection … ACP connection closed` on stderr. Exit code was **0** in every run. | Cosmetic noise; could mask real errors if surfaced. | Observed once in an early throwaway run, not in the final probes. Prefer `session/close` before stdin close in Packet 1; keep stderr in a diagnostic ring buffer (already implemented), never auto-logged. |
| R4 | `fs/*` client capability was never exercised — Claude Code used its own tools. | Workspace containment cannot be enforced through the `fs/*` hook. | Containment must come from the session `cwd` + `additionalDirectories` + permission mode, **not** from `fs/*` interception. `ensure_path_within_roots()` is retained and tested for the paths that do arrive. |
| R5 | `auto` mode silently self-approves. | Hermes' permission model could be bypassed by default. | Packet 3: set mode explicitly at session creation; add a test asserting the mode we requested is the mode reported. |
| R6 | Continuity depends on Claude Code's own session store under `~/.claude`. | External retention/pruning could invalidate a stored ACP session id. | Already designed for: the id is a discardable binding; Packet 2 implements the `session/new` + bounded recap + visible reseed marker fallback and must test the "id no longer loads" path. |
| R7 | Prompt-cache behaviour inside Claude Code is not observable from ACP. | Hermes' "caching is sacred" invariant cannot be verified from this side. | Hermes must not rebuild or replay transcript context per turn; send only the new user turn and let the adapter own history. Packet 1 acceptance check. |
| R8 | Probe consumes real subscription quota and cannot run in CI. | Regressions in the live path are only caught manually. | Keep the fake-agent unit tests as the CI contract; run the live probe on adapter version bumps. |
