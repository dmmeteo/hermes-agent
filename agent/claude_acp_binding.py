"""The binding between a Hermes session and its Claude Code ACP session.

Hermes is authoritative for the conversation: the transcript, the routing, and
the turn lease all live in the Hermes session store. The ACP session id is
**derived, discardable state** — losing it costs a reseed, never a message.
It is therefore stored the way every other per-session runtime marker is
stored (``model_config`` JSON on the ``sessions`` row, the same column that
carries ``browser_model_lock`` / ``yolo_mode`` / ``_delegate_from``), and NOT
in a second journal of its own.

The binding also carries the **turn lease**: a marker saying "a turn is in
flight for this session, held by this process". It exists so a crash cannot
leave a session that looks busy forever, and so two Hermes processes cannot
drive the same Claude Code session — which is how the same turn ends up
delivered twice.

Scope of the lease, stated plainly: it is an advisory guard checked with a
read-modify-write, keyed on process liveness (the same pid + create-time pair
``hermes_cli.active_sessions`` uses for its cross-process session leases). It
is not a distributed mutex; two processes racing the same session row in the
same instant can both observe "free". Hermes runs one live agent per session,
so the realistic failure this defends against is a *stale* lease, and that it
handles exactly.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: model_config key holding the binding. One key, one dict, no new table.
BINDING_KEY = "claude_acp"

#: Lease holder tokens owned by *this* process and genuinely still running.
#: A lease whose token is in here is live even though its pid is our own; one
#: that is not is a leak from a turn that already ended, and is reclaimable.
_LIVE_HOLDERS: set[str] = set()


class TurnLeaseConflict(RuntimeError):
    """Another live process holds this session's turn lease."""


@dataclass
class TurnLease:
    holder: str
    released: bool = False


def _process_start_time(pid: int) -> Optional[float]:
    # Pairing the pid with its create time keeps a recycled pid from making a
    # dead holder look alive. Same helper the active-session registry uses.
    from hermes_cli.active_sessions import _process_start_time as _start

    return _start(pid)


def _holder_is_live(lease: Dict[str, Any]) -> bool:
    holder = str(lease.get("holder") or "")
    if holder and holder in _LIVE_HOLDERS:
        return True
    pid = lease.get("pid")
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int == os.getpid():
        # Our own pid, but not a token we are still holding: a lease whose
        # release never ran. Nothing in this process is driving it.
        return False
    from hermes_cli.active_sessions import _pid_alive

    return bool(_pid_alive(pid_int, lease.get("process_start_time")))


class BindingStore:
    """Read/write the ACP binding for one Hermes session.

    Falls back to process-local memory when there is no session store (a
    one-shot agent, a test harness): continuity within the process still
    works, and there is simply nothing durable to recover from.
    """

    def __init__(self, session_db: Any = None, session_id: str = "") -> None:
        self._db = session_db
        self._session_id = session_id or ""
        self._memory: Dict[str, Any] = {}

    @classmethod
    def for_agent(cls, agent: Any) -> "BindingStore":
        if getattr(agent, "_persist_disabled", False):
            # A persistence-isolated fork (the background review) is pinned to
            # the parent's session_id for prompt-cache reasons but must never
            # write that row — the same rule `_get_session_db_for_recall`
            # enforces for the transcript. Binding to it here would hand the
            # fork the user's Claude Code session, its adapter, and its lease.
            return cls()
        return cls(
            session_db=getattr(agent, "_session_db", None),
            session_id=str(getattr(agent, "session_id", "") or ""),
        )

    @property
    def durable(self) -> bool:
        return self._db is not None and bool(self._session_id)

    # ---------- raw access ----------

    def read(self) -> Dict[str, Any]:
        """The current binding, or ``{}``. Never raises — it is discardable."""
        if not self.durable:
            return dict(self._memory)
        try:
            value = self._db.get_session_model_config_value(
                self._session_id, BINDING_KEY
            )
        except Exception:
            logger.debug("ACP binding read failed", exc_info=True)
            return {}
        return dict(value) if isinstance(value, dict) else {}

    def _write(self, binding: Optional[Dict[str, Any]]) -> None:
        if not self.durable:
            self._memory = dict(binding or {})
            return
        try:
            # A None patch value removes the key — the merge helper's own
            # convention, so lineage markers on the row survive untouched.
            self._db.patch_session_model_config(
                self._session_id, {BINDING_KEY: binding or None}
            )
        except Exception:
            # A binding we cannot persist costs a reseed next process, which
            # is exactly what "discardable" is supposed to mean. Never fail a
            # turn over it.
            logger.warning("ACP binding write failed", exc_info=True)

    # ---------- binding ----------

    def bind(self, acp_session_id: str, cwd: str) -> None:
        binding = self.read()
        binding.update(
            {
                "acp_session_id": acp_session_id,
                "cwd": cwd,
                "updated_at": time.time(),
            }
        )
        self._write(binding)

    def clear(self) -> None:
        self._write(None)

    # ---------- turn lease ----------

    def acquire_turn_lease(self) -> TurnLease:
        """Claim the turn lease, or raise :class:`TurnLeaseConflict`."""
        binding = self.read()
        existing = binding.get("lease")
        if isinstance(existing, dict) and existing:
            if _holder_is_live(existing):
                raise TurnLeaseConflict(
                    "another turn is already running for this Claude Code "
                    "session (held by pid "
                    f"{existing.get('pid')}). Wait for it to finish, or stop it."
                )
            logger.info(
                "Reclaiming a stale Claude Code ACP turn lease (pid %s)",
                existing.get("pid"),
            )

        holder = uuid.uuid4().hex
        _LIVE_HOLDERS.add(holder)
        binding["lease"] = {
            "holder": holder,
            "pid": os.getpid(),
            "process_start_time": _process_start_time(os.getpid()),
            "started_at": time.time(),
        }
        self._write(binding)
        return TurnLease(holder=holder)

    def release_turn_lease(self, lease: Optional[TurnLease]) -> None:
        """Drop the lease. Safe to call twice, and on every failure path."""
        if lease is None or lease.released:
            return
        lease.released = True
        _LIVE_HOLDERS.discard(lease.holder)
        binding = self.read()
        existing = binding.get("lease")
        if isinstance(existing, dict) and existing.get("holder") not in (
            None,
            lease.holder,
        ):
            # Someone else's lease — a reclaim raced us. Leave it alone.
            return
        binding.pop("lease", None)
        self._write(binding)
