import unittest

from web_order_progress import PROGRESS, infer_tracking_progress


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


if __name__ == "__main__":
    unittest.main()
