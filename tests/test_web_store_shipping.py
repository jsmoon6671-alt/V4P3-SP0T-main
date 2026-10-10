import unittest

from web_store import calculate_shipping_fee, option_surcharge, parse_product_options


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


if __name__ == "__main__":
    unittest.main()
