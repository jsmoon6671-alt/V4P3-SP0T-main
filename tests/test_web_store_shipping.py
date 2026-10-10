import unittest

from web_store import calculate_shipping_fee


class WebStoreShippingTests(unittest.TestCase):
    def test_shipping_fee_tiers(self):
        self.assertEqual(calculate_shipping_fee(0), 0)
        self.assertEqual(calculate_shipping_fee(34_999), 3_000)
        self.assertEqual(calculate_shipping_fee(35_000), 1_500)
        self.assertEqual(calculate_shipping_fee(49_999), 1_500)
        self.assertEqual(calculate_shipping_fee(50_000), 0)


if __name__ == "__main__":
    unittest.main()
