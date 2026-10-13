import unittest

from loyalty_points import refund_order_benefits


class RecordingConnection:
    def __init__(self, balance=1_000):
        self.balance = balance
        self.executions = []

    async def execute(self, query, *args):
        self.executions.append((" ".join(query.split()), args))

    async def fetchval(self, query, *args):
        self.executions.append((" ".join(query.split()), args))
        return self.balance


class OrderBenefitRefundTests(unittest.IsolatedAsyncioTestCase):
    async def test_refunds_points_and_releases_coupon_together(self):
        conn = RecordingConnection()
        order = {
            "order_id": "WEB-REFUND-1",
            "guild_id": 123,
            "buyer_id": 456,
            "points_used": 2_000,
            "points_refunded": False,
            "coupon_code": "WELCOME-TEST",
        }

        await refund_order_benefits(conn, order)

        statements = [query for query, _ in conn.executions]
        self.assertTrue(any("balance = balance + $3" in query for query in statements))
        self.assertTrue(any("points_refunded = TRUE" in query for query in statements))
        self.assertTrue(any("web_coupons SET used_order_id = NULL" in query for query in statements))

    async def test_does_not_refund_points_twice_but_still_releases_coupon(self):
        conn = RecordingConnection()
        order = {
            "order_id": "WEB-REFUND-2",
            "guild_id": 123,
            "buyer_id": 456,
            "points_used": 2_000,
            "points_refunded": True,
            "coupon_code": "WELCOME-TEST",
        }

        await refund_order_benefits(conn, order)

        statements = [query for query, _ in conn.executions]
        self.assertFalse(any("balance = balance + $3" in query for query in statements))
        self.assertTrue(any("web_coupons SET used_order_id = NULL" in query for query in statements))


if __name__ == "__main__":
    unittest.main()
