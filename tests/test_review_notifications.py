import unittest

from review_notifications import WEB_REVIEW_BUYER_ROLE_ID, build_web_review_payload


class WebReviewNotificationTests(unittest.TestCase):
    def test_web_review_uses_requested_role_mention_and_components_v2(self):
        payload = build_web_review_payload(5, "싸고 좋네요")
        self.assertEqual(payload["flags"], 1 << 15)
        self.assertEqual(payload["components"][0]["type"], 17)
        self.assertEqual(payload["components"][0]["accent_color"], 0x32CD32)
        content = "\n".join(
            item.get("content", "") for item in payload["components"][0]["components"]
        )
        self.assertIn(f"<@&{WEB_REVIEW_BUYER_ROLE_ID}>", content)
        self.assertNotIn("익명", content)
        self.assertEqual(payload["allowed_mentions"], {
            "parse": [], "roles": [str(WEB_REVIEW_BUYER_ROLE_ID)],
        })

    def test_web_review_images_use_a_v2_media_gallery(self):
        payload = build_web_review_payload(4, "사진 후기입니다", ["one.jpg", "two.jpg"])
        gallery = next(
            item for item in payload["components"][0]["components"] if item["type"] == 12
        )
        self.assertEqual(
            [item["media"]["url"] for item in gallery["items"]],
            ["attachment://one.jpg", "attachment://two.jpg"],
        )


if __name__ == "__main__":
    unittest.main()
