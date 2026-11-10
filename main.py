"""
main.py — runs the three required scenarios plus a bonus chaos run.

    python3 main.py            # all scenarios
    python3 main.py ordering   # just one: ordering | missing | stale | chaos
"""

import logging
import random
import sys

from order_system import (
    APPLIED, REJECTED_STALE_TOKEN, LockService, OrderEvent, OrderStore,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("lab")


def banner(title: str) -> None:
    log.info("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


# --------------------------------------------------------------------------- #
# Scenario 1: version 2, version 1, version 2 again
# --------------------------------------------------------------------------- #
def scenario_ordering() -> None:
    banner("SCENARIO 1 — Versioned ordering: deliver v2, then v1, then v2 again")
    store = OrderStore()
    v1 = OrderEvent("order-1", 1, "CREATED")
    v2 = OrderEvent("order-1", 2, "PAID")
    v3 = OrderEvent("order-1", 3, "SHIPPED")

    for ev in (v2, v1, v2):        # the delivery order the assignment asks for
        store.receive(ev)
    store.receive(v3)              # finish the lifecycle

    log.info("FINAL    %s", store.snapshot("order-1"))
    assert store.orders["order-1"].history == ["CREATED", "PAID", "SHIPPED"]
    log.info("RESULT   PASS: applied in order CREATED -> PAID -> SHIPPED, duplicate ignored")


# --------------------------------------------------------------------------- #
# Scenario 2: later version arrives, earlier one never does
# --------------------------------------------------------------------------- #
def scenario_missing() -> None:
    banner("SCENARIO 2 — Missing event: v1 arrives, v2 is lost, v3 arrives")
    store = OrderStore()
    store.receive(OrderEvent("order-2", 1, "CREATED"))
    store.receive(OrderEvent("order-2", 3, "SHIPPED"))   # v2 never sent

    st = store.orders["order-2"]
    log.info("FINAL    %s", store.snapshot("order-2"))
    assert st.status == "CREATED" and st.version == 1     # did NOT jump to SHIPPED
    assert store.missing_versions("order-2") == [2]
    log.info("RESULT   PASS: order stayed at CREATED; v2 detected as missing, v3 pending")

    log.info("\n-- late arrival: v2 finally shows up --")
    store.receive(OrderEvent("order-2", 2, "PAID"))
    log.info("FINAL    %s", store.snapshot("order-2"))
    assert st.history == ["CREATED", "PAID", "SHIPPED"]
    log.info("RESULT   PASS: once the gap filled, v2 and buffered v3 applied in order")


# --------------------------------------------------------------------------- #
# Scenario 3: two workers, fencing tokens
# --------------------------------------------------------------------------- #
def scenario_stale_worker() -> None:
    banner("SCENARIO 3 — Stale worker: fencing tokens reject the old lease holder")
    store = OrderStore()
    locks = LockService(lease_seconds=5)
    store.receive(OrderEvent("order-3", 1, "CREATED"))

    # t=0  Worker A gets the lock (token 1) ... then freezes (GC pause / network stall)
    a = locks.acquire("order-3", "worker-A", now=0)
    log.info("PAUSE    worker-A stalls (e.g. long GC pause) while holding token %d", a.token)

    # t=2  Worker B tries early: lease still valid, denied
    locks.acquire("order-3", "worker-B", now=2)

    # t=6  A's lease expired. B gets the lock with a NEWER token and writes PAID
    b = locks.acquire("order-3", "worker-B", now=6)
    r_b = store.receive(OrderEvent("order-3", 2, "PAID", fencing_token=b.token))

    # t=7  A wakes up, still believes it owns the lock, and tries to write
    log.info("RESUME   worker-A wakes up, thinks it still holds the lock")
    r_a = store.receive(OrderEvent("order-3", 3, "SHIPPED", fencing_token=a.token))

    log.info("FINAL    %s", store.snapshot("order-3"))
    assert r_b == APPLIED and r_a == REJECTED_STALE_TOKEN
    assert store.orders["order-3"].status == "PAID"
    log.info("RESULT   PASS: worker-B (token %d) updated; worker-A (token %d) rejected",
             b.token, a.token)


# --------------------------------------------------------------------------- #
# Bonus: random order + random duplicates, many times
# --------------------------------------------------------------------------- #
def scenario_chaos(runs: int = 1000, seed: int = 273) -> None:
    banner(f"BONUS — Chaos: {runs} runs of shuffled delivery with random duplicates")
    rng = random.Random(seed)
    lab_log = logging.getLogger("lab")
    lab_log.setLevel(logging.WARNING)     # keep the 1000 runs quiet
    events = [OrderEvent("order-x", 1, "CREATED"),
              OrderEvent("order-x", 2, "PAID"),
              OrderEvent("order-x", 3, "SHIPPED")]
    for _ in range(runs):
        batch = events + rng.choices(events, k=rng.randint(0, 4))  # duplicates
        rng.shuffle(batch)
        store = OrderStore()
        for ev in batch:
            store.receive(ev)
        assert store.orders["order-x"].history == ["CREATED", "PAID", "SHIPPED"], batch
    lab_log.setLevel(logging.INFO)
    log.info("RESULT   PASS: all %d random delivery orders converged to "
             "CREATED -> PAID -> SHIPPED", runs)


SCENARIOS = {
    "ordering": scenario_ordering,
    "missing": scenario_missing,
    "stale": scenario_stale_worker,
    "chaos": scenario_chaos,
}

if __name__ == "__main__":
    chosen = sys.argv[1:] or list(SCENARIOS)
    for name in chosen:
        SCENARIOS[name]()
