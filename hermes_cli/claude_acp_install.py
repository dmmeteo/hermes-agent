"""Managed install for the Claude Code ACP adapter.

The adapter is a Node binary. Installing it globally with the user's system
npm is exactly the "raw env var / raw global npm" advice AGENTS.md steers away
from, and a global install may not even be on PATH for a Hermes-managed Node.
So we install it into the Hermes-managed prefix (``$HERMES_HOME/node``) the
same way ``install.sh`` installs agent-browser, and resolve it from there via
``find_node_executable``.

The version pin itself lives in one place — ``hermes_cli.auth`` — and is
imported here and by ``scripts/claude_acp_probe.py``.
"""

from __future__ import annotations

import logging
import subprocess

from hermes_constants import find_node_executable, get_hermes_home
from tools.environments.local import hermes_subprocess_env

logger = logging.getLogger(__name__)

ADAPTER_COMMAND = "claude-agent-acp"


def adapter_path() -> str | None:
    """Resolved adapter path, preferring PATH then a Hermes-managed install.

    Uses the same plain managed-dir scan as provider resolution — see
    ``hermes_cli.auth._find_in_managed_node_dirs`` for why this must not go
    through the Node-tooling resolver.
    """
    import shutil

    from hermes_cli.auth import _find_in_managed_node_dirs

    return shutil.which(ADAPTER_COMMAND) or _find_in_managed_node_dirs(ADAPTER_COMMAND)


def install_adapter(timeout: float = 600.0) -> tuple[bool, str]:
    """Install the pinned adapter into ``$HERMES_HOME/node``.

    Returns ``(ok, message)``. Never raises — callers are interactive setup
    flows that must degrade to an actionable message.
    """
    from hermes_cli.auth import CLAUDE_CODE_ACP_PACKAGE

    npm = find_node_executable("npm")
    if not npm:
        return False, (
            "Node.js/npm was not found. Install Node 22+ (or run `hermes setup`) "
            "and try again."
        )

    prefix = get_hermes_home() / "node"
    argv = [npm, "install", "-g", "--prefix", str(prefix), CLAUDE_CODE_ACP_PACKAGE]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=hermes_subprocess_env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"npm install timed out after {timeout:.0f}s."
    except Exception as exc:
        return False, f"npm install could not be started: {exc}"

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return False, "npm install failed: " + " / ".join(tail)

    resolved = adapter_path()
    if not resolved:
        return False, (
            f"npm reported success but {ADAPTER_COMMAND} is still not resolvable "
            f"under {prefix}."
        )
    return True, resolved
