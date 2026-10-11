import unittest

from web_store import (
    ADMIN_USER_ID,
    StoreError,
    calculate_shipping_fee,
    normalize_option_groups,
    normalize_selected_options,
    option_surcharge,
    parse_product_options,
    product_selection_surcharge,
    selected_options_surcharge,
)


class WebStoreShippingTests(unittest.TestCase):
    def test_shipping_fee_tiers(self):
        self.assertEqual(calculate_shipping_fee(0), 0)
        self.assertEqual(calculate_shipping_fee(34_999), 3_000)
        self.assertEqual(calculate_shipping_fee(35_000), 1_500)
        self.assertEqual(calculate_shipping_fee(49_999), 1_500)
        self.assertEqual(calculate_shipping_fee(50_000), 0)

    def test_option_surcharge_format(self):
        self.assertEqual(option_surcharge("블랙 (+2000)"), 2_000)
        self.assertEqual(option_surcharge("프리미엄 (+2,500원)"), 2_500)
        self.assertEqual(option_surcharge("기본 패키지"), 0)

    def test_option_parser_preserves_price_commas(self):
        self.assertEqual(
            parse_product_options("기본 패키지, 선물 패키지 (+2,000원)\n선물 패키지 (+2,000원)"),
            ["기본 패키지", "선물 패키지 (+2,000원)"],
        )

    def test_three_option_groups_include_concentration(self):
        groups = normalize_option_groups([
            {"label": "맛", "options": "망고\n포도"},
            {"label": "색상", "options": ["블랙", "화이트"]},
            {"label": "농도", "options": "3mg\n6mg (+1000)"},
        ])
        self.assertEqual([group["label"] for group in groups], ["맛", "색상", "농도"])
        self.assertEqual(selected_options_surcharge(["망고", "블랙", "6mg (+1000)"]), 1_000)

    def test_more_than_three_option_groups_are_rejected(self):
        with self.assertRaises(StoreError):
            normalize_option_groups([
                {"label": "옵션", "options": [str(index)]}
                for index in range(4)
            ])

    def test_custom_option_label_is_allowed(self):
        groups = normalize_option_groups([
            {"label": "코일 종류", "options": "0.6옴\n0.8옴 (+1000)"},
        ])
        self.assertEqual(groups[0]["label"], "코일 종류")
        self.assertEqual(groups[0]["options"], ["0.6옴", "0.8옴 (+1000)"])

    def test_option_groups_must_be_created_from_first_to_third(self):
        with self.assertRaisesRegex(StoreError, "1번부터"):
            normalize_option_groups([
                {"label": "없음", "options": []},
                {"label": "색상", "options": ["블랙"]},
            ])

    def test_buyer_text_question_requires_an_answer(self):
        groups = normalize_option_groups([
            {"type": "text", "label": "각인할 문구를 입력해 주세요", "options": []},
        ])
        self.assertEqual(groups, [{"type": "text", "label": "각인할 문구를 입력해 주세요", "options": []}])
        self.assertEqual(normalize_selected_options(groups, ["V4P3 SP0T"]), ["V4P3 SP0T"])
        with self.assertRaisesRegex(StoreError, "답변"):
            normalize_selected_options(groups, [""])

    def test_buyer_text_answer_never_adds_a_surcharge(self):
        groups = normalize_option_groups([
            {"type": "text", "label": "요청사항", "options": []},
            {"label": "패키지", "options": ["기본", "선물 (+2000)"]},
        ])
        self.assertEqual(product_selection_surcharge(groups, ["문구 (+9000)", "선물 (+2000)"]), 2_000)

    def test_admin_account_is_the_configured_discord_user(self):
        self.assertEqual(ADMIN_USER_ID, 259758966043574272)


if __name__ == "__main__":
    unittest.main()
