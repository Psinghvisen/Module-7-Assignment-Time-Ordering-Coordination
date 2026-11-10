"""Unit tests — run with:  python3 -m unittest -v"""

import logging
import unittest

from order_system import (
    APPLIED, BUFFERED, DUPLICATE, REJECTED_INVALID, REJECTED_STALE_TOKEN,
    LockService, OrderEvent, OrderStore,
)

logging.getLogger("lab").setLevel(logging.CRITICAL)

V1 = OrderEvent("o", 1, "CREATED")
V2 = OrderEvent("o", 2, "PAID")
V3 = OrderEvent("o", 3, "SHIPPED")


class OrderingTests(unittest.TestCase):
    def test_v2_v1_v2_again(self):
        s = OrderStore()
        self.assertEqual(s.receive(V2), BUFFERED)
        self.assertEqual(s.receive(V1), APPLIED)        # applies v1 then drains v2
        self.assertEqual(s.receive(V2), DUPLICATE)
        self.assertEqual(s.orders["o"].history, ["CREATED", "PAID"])

    def test_duplicate_while_buffered_is_ignored(self):
        s = OrderStore()
        s.receive(V3)
        self.assertEqual(s.receive(V3), DUPLICATE)
        self.assertEqual(len(s.orders["o"].pending), 1)

    def test_invalid_status_for_version_rejected(self):
        s = OrderStore()
        self.assertEqual(s.receive(OrderEvent("o", 1, "SHIPPED")), REJECTED_INVALID)


class MissingEventTests(unittest.TestCase):
    def test_does_not_jump_ahead(self):
        s = OrderStore()
        s.receive(V1)
        s.receive(V3)
        self.assertEqual(s.orders["o"].status, "CREATED")
        self.assertEqual(s.missing_versions("o"), [2])
        self.assertIn(3, s.orders["o"].pending)

    def test_gap_fills_later(self):
        s = OrderStore()
        for ev in (V3, V1, V2):
            s.receive(ev)
        self.assertEqual(s.orders["o"].history, ["CREATED", "PAID", "SHIPPED"])
        self.assertEqual(s.missing_versions("o"), [])


class FencingTests(unittest.TestCase):
    def test_tokens_increase(self):
        locks = LockService(lease_seconds=5)
        a = locks.acquire("o", "A", now=0)
        self.assertIsNone(locks.acquire("o", "B", now=1))    # lease still held
        b = locks.acquire("o", "B", now=6)
        self.assertGreater(b.token, a.token)

    def test_stale_token_rejected(self):
        s = OrderStore()
        s.receive(V1)
        self.assertEqual(s.receive(OrderEvent("o", 2, "PAID", fencing_token=2)), APPLIED)
        self.assertEqual(
            s.receive(OrderEvent("o", 3, "SHIPPED", fencing_token=1)),
            REJECTED_STALE_TOKEN,
        )
        self.assertEqual(s.orders["o"].status, "PAID")


if __name__ == "__main__":
    unittest.main()
