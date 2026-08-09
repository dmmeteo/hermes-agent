#!/usr/bin/env python3
"""Live feasibility probe for the Claude Code ACP adapter (Packet 0).

Drives the REAL adapter over REAL stdio using the retained
``agent.transports.claude_acp.ClaudeAcpClient`` and reports what the protocol
actually supports. Not part of the pytest suite: it spawns a real Claude Code
session and consumes subscription quota.

Every prompt it sends is read-only, and every permission request it receives
is DENIED, so the probe cannot modify the workspace.

Usage::

    python scripts/claude_acp_probe.py --workspace /tmp/acp-probe
    python scripts/claude_acp_probe.py --workspace /tmp/acp-probe --probe-cancel
    python scripts/claude_acp_probe.py --workspace /tmp/acp-probe --probe-permission

Adapter resolution, in order:
  1. ``--command`` / ``HERMES_CLAUDE_ACP_COMMAND`` (shell-style argv string)
  2. ``claude-agent-acp`` on PATH
  3. ``npx -y @agentclientprotocol/claude-agent-acp@<pinned>``
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.redact import redact_sensitive_text  # noqa: E402
from hermes_cli.auth import CLAUDE_CODE_ACP_PACKAGE  # noqa: E402
from agent.transports.claude_acp import (  # noqa: E402
    AcpError,
    AcpProtocolError,
    ClaudeAcpClient,
    deny_permission_response,
    ensure_path_within_roots,
)

# Pinned adapter version this probe was validated against. Single source of
# truth lives in hermes_cli.auth so the probe and the install hint cannot drift.
PINNED_ADAPTER = CLAUDE_CODE_ACP_PACKAGE

READ_ONLY_PROMPT = (
    "Read the file hello.txt in the current directory and reply with only the "
    "number it contains. Do not modify anything."
)
WRITE_PROMPT = (
    "Create a file named probe-should-not-exist.txt containing the word test."
)


def resolve_command(explicit: str | None) -> list[str]:
    raw = explicit or os.getenv("HERMES_CLAUDE_ACP_COMMAND", "").strip()
    if raw:
        return shlex.split(raw)
    found = shutil.which("claude-agent-acp")
    if found:
        return [found]
    return ["npx", "-y", PINNED_ADAPTER]


def make_request_handler(roots: list[str], log: list[str]):
    """Deny every permission; serve reads only from inside the workspace."""

    def handler(method: str, params: dict) -> dict:
        log.append(method)
        if method == "session/request_permission":
            return deny_permission_response(params.get("options") or [])
        if method == "fs/read_text_file":
            path = ensure_path_within_roots(params.get("path", ""), roots)
            return {"content": path.read_text(encoding="utf-8")}
        raise RuntimeError(f"probe does not implement {method}")

    return handler


def run_turn(client, session_id, text, roots, *, cancel_after=None, label=""):
    updates: Counter = Counter()
    chunks: list[str] = []
    requests: list[str] = []
    handler = make_request_handler(roots, requests)
    started = time.time()

    def on_update(update: dict) -> None:
        kind = update.get("sessionUpdate", "?")
        updates[kind] += 1
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text":
                chunks.append(content.get("text", ""))
        if cancel_after is not None and time.time() - started >= cancel_after:
            client.cancel(session_id)

    result = client.prompt(
        session_id,
        [{"type": "text", "text": text}],
        on_update=on_update,
        on_request=handler,
    )
    elapsed = time.time() - started

    print(f"\n--- turn{(' ' + label) if label else ''} ---")
    print(f"stopReason:      {result.get('stopReason')}")
    print(f"elapsed:         {elapsed:.1f}s")
    print(f"update kinds:    {json.dumps(dict(updates))}")
    print(f"client requests: {requests or '[]'}")
    print(f"agent text:      {''.join(chunks)[:400]!r}")
    return result, updates, requests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", required=True,
                        help="scratch directory to use as the session cwd")
    parser.add_argument("--command", default=None,
                        help="adapter argv (shell-quoted); overrides autodetection")
    parser.add_argument("--probe-cancel", action="store_true",
                        help="run an extra turn and cancel it mid-flight")
    parser.add_argument("--probe-permission", action="store_true",
                        help="switch to manual mode and DENY a write request")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    workspace = os.path.abspath(args.workspace)
    os.makedirs(workspace, exist_ok=True)
    marker = os.path.join(workspace, "hello.txt")
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("hermes-acp-spike-marker: 42\n")
    roots = [workspace]

    command = resolve_command(args.command)
    print(f"adapter command: {' '.join(command)}")
    print(f"workspace:       {workspace}")
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        print(f"{var}: {'SET' if os.getenv(var) else 'unset'}")

    client = ClaudeAcpClient(command=command, cwd=workspace)
    failures: list[str] = []
    try:
        print("\n=== initialize ===")
        init = client.initialize(client_capabilities={
            "fs": {"readTextFile": True, "writeTextFile": False},
            "terminal": False,
        }, timeout=args.timeout)
        print(f"protocolVersion:   {client.protocol_version}")
        print(f"authMethods:       {json.dumps(client.auth_methods)}")
        print(f"loadSession:       {client.supports_load_session}")
        print(f"sessionCaps:       {json.dumps(sorted(client.session_capabilities))}")
        print(f"agentCapabilities: {json.dumps(init.get('agentCapabilities'), indent=2)}")

        print("\n=== session/new ===")
        session_id = client.session_new(timeout=args.timeout)
        print(f"sessionId: {session_id}")

        print("\n=== session/prompt (read-only) ===")
        result, updates, _ = run_turn(client, session_id, READ_ONLY_PROMPT, roots,
                                      label="read-only")
        if result.get("stopReason") != "end_turn":
            failures.append(f"read-only turn stopReason={result.get('stopReason')}")
        if not updates:
            failures.append("no typed session/update events received")

        if args.probe_permission:
            print("\n=== permission probe (manual mode, request DENIED) ===")
            try:
                client.request("session/set_mode",
                               {"sessionId": session_id, "modeId": "default"},
                               timeout=30)
                print("mode -> default (manual)")
            except (AcpError, AcpProtocolError) as exc:
                print(f"session/set_mode unavailable: {exc}")
            _, _, requests = run_turn(client, session_id, WRITE_PROMPT, roots,
                                      label="write-denied")
            print(f"permission requests seen: "
                  f"{[r for r in requests if r == 'session/request_permission']}")
            leaked = os.path.exists(os.path.join(workspace, "probe-should-not-exist.txt"))
            print(f"file created despite denial: {leaked}")
            if leaked:
                failures.append("denied write still created a file")

        if args.probe_cancel:
            print("\n=== cancellation probe ===")
            cancel_result, _, _ = run_turn(
                client, session_id,
                "Count slowly from 1 to 200, one number per line.",
                roots, cancel_after=3.0, label="cancelled",
            )
            print(f"cancel stopReason: {cancel_result.get('stopReason')}")

        print("\n=== session/load probe ===")
        if client.supports_load_session:
            replayed: Counter = Counter()
            try:
                client.session_load(
                    session_id,
                    on_update=lambda u: replayed.update([u.get("sessionUpdate", "?")]),
                    timeout=args.timeout,
                )
                print(f"session/load OK; replayed: {json.dumps(dict(replayed))}")
            except (AcpError, AcpProtocolError) as exc:
                print(f"session/load FAILED: {exc}")
                failures.append(f"session/load failed: {exc}")
        else:
            print("agent does not advertise loadSession")

    finally:
        print("\n=== clean stop ===")
        client.close()
        print(f"exit code: {client.returncode}")
        tail = client.stderr_tail()
        if tail:
            print("stderr tail (redacted):")
            for line in tail[-10:]:
                print(f"  {redact_sensitive_text(line, force=True)}")

    print("\n=== VERDICT ===")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: real prompt completed over ACP with typed events and a clean stop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
