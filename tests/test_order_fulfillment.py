import unittest

from order_fulfillment import format_buyer_order_items
from order_editing import build_purchase_log_payload


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
        formatted = format_buyer_order_items([{
            "product_name": "테스트 상품",
            "quantity": 2,
            "product_options_json": '["블랙", "멘솔"]',
            "option_groups_json": '[{"label":"색상"},{"label":"맛"}]',
        }])
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
        self.assertNotIn("기존 합쳐진 상품명", content)


if __name__ == "__main__":
    unittest.main()
