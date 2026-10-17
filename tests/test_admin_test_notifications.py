import unittest

from admin_test_notifications import TEST_NOTIFICATION_TYPES, build_test_notification


class AdminTestNotificationTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            "outcome": "APPROVED",
            "product": "테스트 기기",
            "options": "색상: 클리어",
            "quantity": 2,
            "amount": 35_000,
            "order_id": "TEST-WEB-ABC123",
            "message": "테스트 문의입니다.",
        }

    def test_every_test_type_builds_a_non_destructive_payload(self):
        for kind in TEST_NOTIFICATION_TYPES:
            with self.subTest(kind=kind):
                result = build_test_notification(kind, self.data, 259758966043574272)
                self.assertIn("payload", result)
                self.assertIn("테스트", result["preview"])
                self.assertEqual(result["payload"]["flags"], 1 << 15)
                self.assertEqual(result["payload"]["allowed_mentions"].get("parse"), [])

    def test_purchase_supports_approved_rejected_and_expired_results(self):
        labels = {
            "APPROVED": "승인",
            "REJECTED": "거절",
            "EXPIRED": "시간초과",
        }
        for outcome, label in labels.items():
            with self.subTest(outcome=outcome):
                data = {**self.data, "outcome": outcome}
                result = build_test_notification("purchase", data, 259758966043574272)
                self.assertIn(label, result["preview"])

    def test_dm_types_are_marked_for_direct_delivery(self):
        self.assertTrue(build_test_notification("receipt", self.data, 259758966043574272)["dm"])
        self.assertTrue(build_test_notification("shipping", self.data, 259758966043574272)["dm"])
        self.assertFalse(build_test_notification("purchase_log", self.data, 259758966043574272)["dm"])

    def test_unknown_type_is_rejected(self):
        with self.assertRaises(ValueError):
            build_test_notification("unknown", self.data, 259758966043574272)


if __name__ == "__main__":
    unittest.main()
