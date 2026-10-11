import unittest

from web_order_progress import PROGRESS, infer_tracking_progress, latest_significant_tracking_event


class WebOrderProgressTests(unittest.TestCase):
    def test_progress_percentages_are_monotonic(self):
        self.assertEqual(
            [PROGRESS[key][1] for key in (
                "PAYMENT_APPROVED", "PRODUCT_PREPARING", "SHIPPING_PREPARING", "SHIPPING", "DELIVERED",
            )],
            [20, 40, 60, 80, 100],
        )

    def test_delivery_complete_is_detected_from_history(self):
        data = {
            "status": "배송 중",
            "allProgress": [{"description": "고객에게 상품 전달완료"}],
        }
        self.assertEqual(infer_tracking_progress(data), "DELIVERED")

    def test_shipping_and_pickup_are_classified(self):
        self.assertEqual(infer_tracking_progress({"status": "간선상차"}), "SHIPPING")
        self.assertEqual(infer_tracking_progress({"status": "상품인수"}), "SHIPPING_PREPARING")

    def test_store_arrival_is_delivery_complete(self):
        data = {"status": "배송중", "allProgress": [{"description": "상품이 점포에 도착했습니다."}]}
        self.assertEqual(infer_tracking_progress(data), "DELIVERED")
        self.assertEqual(infer_tracking_progress({"status": "점포에 도착 하였습니다."}), "DELIVERED")

    def test_latest_arrival_event_is_selected_for_notification(self):
        data = {"allProgress": [
            {"time": "1", "location": "A센터", "description": "A센터에 도착했습니다."},
            {"time": "2", "location": "진주저온센터", "description": "진주저온센터에 도착했습니다."},
        ]}
        signature, message = latest_significant_tracking_event(data)
        self.assertTrue(signature.startswith("2|"))
        self.assertIn("진주저온센터", message)


if __name__ == "__main__":
    unittest.main()
