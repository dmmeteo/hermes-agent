"""Workspace roots for a Claude Code ACP session — what the adapter may touch.

Packet 0 (spike risk R4) settled how containment has to work here: the adapter
runs Claude Code's own tools inside its own process and reads files with its
native ``Read`` rather than calling back into the client, so intercepting
``fs/*`` contains nothing. Containment is therefore **negotiated at session
creation**: ``session/new`` carries a ``cwd`` and, when the agent advertises the
capability, the ``additionalDirectories`` the user explicitly allowed. Those
same roots are then the yardstick for any request the agent *does* send back.

Two rules shape everything below:

* **Explicit only.** The extra roots come from one config key
  (``model.claude_code_acp.additional_directories``) — never an env var, never
  an inference from the workspace layout. A directory nobody named is not a
  root.
* **Every rejection narrows.** A relative path, a missing directory, a file, a
  duplicate of the workspace: all dropped rather than forwarded. Dropping a root
  can only ever make the session smaller, while forwarding a path nobody
  verified is how a session quietly widens.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, List, Sequence

logger = logging.getLogger(__name__)

#: Where the user names extra directories. AGENTS.md: non-secret settings live
#: in config.yaml, so there is deliberately no env-var equivalent.
CONFIG_KEYS = ("model", "claude_code_acp", "additional_directories")

#: Upper bound on negotiated extra roots. A prompt the user cannot read is a
#: prompt the user cannot audit, and a runaway config should not turn into an
#: unbounded session scope.
MAX_ADDITIONAL_DIRECTORIES = 16


def configured_additional_directories() -> List[str]:
    """Read the raw ``additional_directories`` list from config.yaml.

    Never raises: a broken or unreadable config yields *no* extra roots, which
    is the narrow answer.
    """
    try:
        from hermes_cli.config import load_config

        node: Any = load_config()
        for key in CONFIG_KEYS:
            if not isinstance(node, dict):
                return []
            node = node.get(key)
    except Exception:
        logger.debug("reading Claude Code ACP workspace roots failed", exc_info=True)
        return []
    if isinstance(node, str):
        node = [node]
    if not isinstance(node, (list, tuple)):
        return []
    return [entry for entry in node if isinstance(entry, str) and entry.strip()]


def resolve_additional_directories(
    cwd: str, configured: Sequence[str] | None = None
) -> List[str]:
    """The extra roots to negotiate alongside *cwd*, in config order.

    Each entry must be an absolute path to an existing directory that is not the
    workspace itself and not already inside it. Everything else is dropped with a
    warning — the user asked for something we cannot honour as a root, and
    guessing what they meant would widen the session on their behalf.
    """
    raw = list(configured) if configured is not None else configured_additional_directories()
    if not raw:
        return []

    try:
        workspace = Path(cwd).expanduser().resolve()
    except Exception:
        logger.debug("workspace path could not be resolved: %r", cwd, exc_info=True)
        workspace = None

    roots: List[str] = []
    for entry in raw:
        text = entry.strip()
        if not text:
            continue
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            # A relative root resolves against whatever the process cwd happens
            # to be, which is not a directory the user named.
            logger.warning(
                "Ignoring Claude Code workspace directory %r: not an absolute path",
                text,
            )
            continue
        try:
            resolved = candidate.resolve()
            if not resolved.is_dir():
                raise NotADirectoryError(str(resolved))
        except Exception:
            logger.warning(
                "Ignoring Claude Code workspace directory %r: not an existing "
                "directory on this host", text,
            )
            continue
        if workspace is not None and (
            resolved == workspace or workspace in resolved.parents
        ):
            # Already covered by the session cwd. Sending it again adds no scope
            # and only makes the negotiated root list harder to audit.
            continue
        text_resolved = str(resolved)
        if text_resolved not in roots:
            roots.append(text_resolved)

    if len(roots) > MAX_ADDITIONAL_DIRECTORIES:
        logger.warning(
            "Claude Code workspace: keeping the first %d of %d configured "
            "additional directories", MAX_ADDITIONAL_DIRECTORIES, len(roots),
        )
        roots = roots[:MAX_ADDITIONAL_DIRECTORIES]
    return roots


def resolve_within_roots(path_text: str, roots: Iterable[str]) -> Path:
    """Resolve *path_text* and require it to sit inside one of *roots*.

    Resolved-parent comparison, never a string prefix: ``/ws-evil`` must not
    pass a ``/ws`` root, and ``..`` must not escape one.
    """
    resolved = Path(path_text).expanduser().resolve()
    for root in roots or ():
        if not root:
            continue
        root_resolved = Path(root).expanduser().resolve()
        if resolved == root_resolved or root_resolved in resolved.parents:
            return resolved
    raise PermissionError(f"path outside the permitted workspace: {path_text}")


def is_within_roots(path_text: str, roots: Iterable[str]) -> bool:
    """Whether *path_text* is inside one of *roots*. Never raises.

    Used for display classification, where an unresolvable path must not break
    the prompt it appears in. Anything we cannot place is reported as outside —
    the safe direction for a warning.
    """
    if not isinstance(path_text, str) or not path_text.strip():
        return False
    try:
        resolve_within_roots(path_text, roots)
    except Exception:
        return False
    return True
