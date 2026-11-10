"""
order_system.py — the core of the lab.

Three building blocks:
  1. OrderEvent     : one message on the wire (order_id, version, status, fencing_token)
  2. OrderStore     : the "database" that applies events strictly in version order,
                      buffers early arrivals, and ignores duplicates
  3. LockService    : hands out leases with monotonically increasing fencing tokens

The store also enforces fencing: it remembers the highest token it has ever seen
for each order and rejects any write that carries an older token.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger("lab")

# The legal state machine: version N must carry exactly this status.
STATE_FOR_VERSION = {1: "CREATED", 2: "PAID", 3: "SHIPPED"}


# --------------------------------------------------------------------------- #
# 1. The message
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OrderEvent:
    order_id: str
    version: int
    status: str
    fencing_token: Optional[int] = None  # only set when a worker sends it

    def __str__(self) -> str:
        tok = f", token={self.fencing_token}" if self.fencing_token is not None else ""
        return f"{self.order_id} v{self.version} {self.status}{tok}"


# --------------------------------------------------------------------------- #
# 2. Per-order state kept by the store
# --------------------------------------------------------------------------- #
@dataclass
class OrderState:
    version: int = 0                      # last APPLIED version (0 = nothing yet)
    status: Optional[str] = None          # current status
    pending: Dict[int, OrderEvent] = field(default_factory=dict)  # early arrivals
    highest_token: int = 0                # highest fencing token ever accepted
    history: List[str] = field(default_factory=list)  # applied statuses, in order


# Result codes returned by OrderStore.receive(), handy for tests & logs.
APPLIED = "APPLIED"
BUFFERED = "BUFFERED"
DUPLICATE = "DUPLICATE"
REJECTED_STALE_TOKEN = "REJECTED_STALE_TOKEN"
REJECTED_INVALID = "REJECTED_INVALID"


class OrderStore:
    def __init__(self) -> None:
        self.orders: Dict[str, OrderState] = {}

    def _state(self, order_id: str) -> OrderState:
        return self.orders.setdefault(order_id, OrderState())

    # ---- the single entry point every event goes through -------------------
    def receive(self, ev: OrderEvent) -> str:
        st = self._state(ev.order_id)
        log.info("RECV     %-32s | current=v%d %s", ev, st.version, st.status)

        # (a) Fencing check: an older token means a stale worker. Reject.
        if ev.fencing_token is not None:
            if ev.fencing_token < st.highest_token:
                log.warning(
                    "REJECT   %-32s | stale token %d < highest seen %d",
                    ev, ev.fencing_token, st.highest_token,
                )
                return REJECTED_STALE_TOKEN
            st.highest_token = ev.fencing_token

        # (b) Sanity: version must map to the right status in the state machine.
        if STATE_FOR_VERSION.get(ev.version) != ev.status:
            log.warning("REJECT   %-32s | invalid version/status pair", ev)
            return REJECTED_INVALID

        # (c) Duplicate: already applied, or already waiting in the buffer.
        if ev.version <= st.version or ev.version in st.pending:
            log.info("IGNORE   %-32s | duplicate (already have v%d)", ev, ev.version)
            return DUPLICATE

        # (d) Gap: arrived early. Park it and say what we are waiting for.
        if ev.version > st.version + 1:
            st.pending[ev.version] = ev
            missing = self.missing_versions(ev.order_id)
            log.info(
                "BUFFER   %-32s | waiting for missing v%s; pending=%s",
                ev, ",".join(map(str, missing)), sorted(st.pending),
            )
            return BUFFERED

        # (e) Exactly the next version: apply it, then drain anything now unblocked.
        self._apply(st, ev)
        while st.version + 1 in st.pending:
            nxt = st.pending.pop(st.version + 1)
            log.info("DRAIN    %-32s | gap filled, applying buffered event", nxt)
            self._apply(st, nxt)
        return APPLIED

    def _apply(self, st: OrderState, ev: OrderEvent) -> None:
        st.version, st.status = ev.version, ev.status
        st.history.append(ev.status)
        log.info("APPLY    %-32s | order now v%d %s", ev, st.version, st.status)

    # ---- helpers used by the demo and tests ---------------------------------
    def missing_versions(self, order_id: str) -> List[int]:
        st = self._state(order_id)
        if not st.pending:
            return []
        top = max(st.pending)
        return [v for v in range(st.version + 1, top) if v not in st.pending]

    def snapshot(self, order_id: str) -> str:
        st = self._state(order_id)
        return (
            f"{order_id}: v{st.version} {st.status} | history={st.history} "
            f"| pending={sorted(st.pending)} | missing={self.missing_versions(order_id)}"
        )


# --------------------------------------------------------------------------- #
# 3. Lock service with fencing tokens
# --------------------------------------------------------------------------- #
@dataclass
class Lease:
    worker: str
    token: int
    expires_at: float


class LockService:
    """Grants one lease per order at a time. Every grant gets a bigger token."""

    def __init__(self, lease_seconds: float = 5.0) -> None:
        self.lease_seconds = lease_seconds
        self._next_token = 0
        self._leases: Dict[str, Lease] = {}

    def acquire(self, order_id: str, worker: str, now: float) -> Optional[Lease]:
        cur = self._leases.get(order_id)
        if cur and cur.expires_at > now:
            log.info("LOCK     %s denied to %s (held by %s until t=%.0f)",
                     order_id, worker, cur.worker, cur.expires_at)
            return None
        self._next_token += 1
        lease = Lease(worker, self._next_token, now + self.lease_seconds)
        self._leases[order_id] = lease
        log.info("LOCK     %s granted to %s with fencing token %d (expires t=%.0f)",
                 order_id, worker, lease.token, lease.expires_at)
        return lease
