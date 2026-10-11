import unittest

from web_store import (
    StoreError,
    calculate_shipping_fee,
    normalize_option_groups,
    option_surcharge,
    parse_product_options,
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


if __name__ == "__main__":
    unittest.main()
