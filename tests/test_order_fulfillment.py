import datetime
import unittest

from order_fulfillment import build_order_receipt, format_buyer_order_items
from order_editing import build_purchase_log_payload, format_order_items_for_discord


class BuyerOrderItemFormattingTests(unittest.TestCase):
    def test_each_selected_option_is_displayed_with_its_own_label(self):
        text = format_buyer_order_items([{
            "product_name": "테스트 상품",
            "quantity": 2,
            "product_options_json": '["블랙", "멘솔", "문 앞에 놓아주세요"]',
            "option_groups_json": '[{"label":"색상"},{"label":"맛"},{"type":"text","label":"요청사항"}]',
        }])

        self.assertIn("• 테스트 상품 × 2", text)
        self.assertIn("└ 색상: 블랙", text)
        self.assertIn("└ 맛: 멘솔", text)
        self.assertIn("└ 요청사항: 문 앞에 놓아주세요", text)

    def test_purchase_log_uses_the_separated_item_options(self):
        formatted = format_order_items_for_discord([{
            "product_name": "테스트 상품",
            "quantity": 2,
            "product_options_json": '["블랙", "멘솔", "문 앞에 놓아주세요"]',
            "option_groups_json": '[{"label":"색상"},{"label":"맛"},{"type":"text","label":"요청사항"}]',
        }], include_text_answers=False)
        payload = build_purchase_log_payload({
            "order_id": "WEB-TEST",
            "product": "기존 합쳐진 상품명",
            "quantity": "2개",
            "amount": "20,000원",
            "points_used": 0,
            "cash_amount": 20_000,
            "_formatted_items": formatted,
        }, None)
        content = payload["components"][0]["components"][0]["content"]

        self.assertIn("└ 색상: 블랙", content)
        self.assertIn("└ 맛: 멘솔", content)
        self.assertNotIn("요청사항", content)
        self.assertNotIn("문 앞에 놓아주세요", content)
        self.assertNotIn("기존 합쳐진 상품명", content)

    def test_buyer_receipt_contains_options_and_payment_breakdown(self):
        receipt = build_order_receipt({
            "order_id": "WEB-RECEIPT",
            "product": "기존 상품명",
            "quantity": "총 2개",
            "amount": "20,000원",
            "shipping_fee": 3_000,
            "coupon_discount": 2_000,
            "points_used": 1_000,
            "cash_amount": 19_000,
        }, [{
            "product_name": "테스트 상품",
            "quantity": 2,
            "product_options_json": '["블랙", "멘솔"]',
            "option_groups_json": '[{"label":"색상"},{"label":"맛"}]',
        }], approved_at=datetime.datetime(2026, 10, 14, 12, 30, 0))

        self.assertIn("V4P3 SP0T 구매 영수증", receipt)
        self.assertIn("WEB-RECEIPT", receipt)
        self.assertIn("색상: 블랙", receipt)
        self.assertIn("맛: 멘솔", receipt)
        self.assertIn("쿠폰 할인` -2,000원", receipt)
        self.assertIn("사용 포인트` 1,000P", receipt)
        self.assertIn("실제 입금액` 19,000원", receipt)


if __name__ == "__main__":
    unittest.main()
