import os
import unittest
from unittest.mock import patch

from payment_automation import normalize_depositor, parse_deposit_notice


def mirror(title, body, *, notification_id="1", package="com.bank.app"):
    return {
        "type": "mirror",
        "title": title,
        "body": body,
        "application_name": "테스트은행",
        "package_name": package,
        "source_device_iden": "phone-1",
        "notification_id": notification_id,
        "created": 100.25,
    }


class PushbulletParserTests(unittest.TestCase):
    def test_parses_sender_then_amount(self):
        result = parse_deposit_notice(mirror("입금 알림", "입금자: 홍길동 12,300원 입금"))
        self.assertEqual(result.depositor_name, "홍길동")
        self.assertEqual(result.amount, 12300)

    def test_parses_toss_style_notification(self):
        result = parse_deposit_notice(mirror("토스뱅크", "김철수님이 25,000원을 보냈어요"))
        self.assertEqual(result.depositor_name, "김철수")
        self.assertEqual(result.amount, 25000)

    def test_parses_amount_then_sender(self):
        result = parse_deposit_notice(mirror("입금", "8,500원 입금 박영희"))
        self.assertEqual((result.depositor_name, result.amount), ("박영희", 8500))

    def test_ignores_outgoing_and_non_mirror_notifications(self):
        self.assertIsNone(parse_deposit_notice(mirror("출금", "10,000원 출금 홍길동")))
        self.assertIsNone(parse_deposit_notice({"type": "note", "title": "입금", "body": "홍길동 1,000원"}))

    def test_custom_bank_regex(self):
        pattern = r"받는분=(?P<name>[가-힣]+).*금액=(?P<amount>[0-9,]+)"
        result = parse_deposit_notice(mirror("은행", "받는분=홍길동 / 금액=77,000"), pattern=pattern)
        self.assertEqual((result.depositor_name, result.amount), ("홍길동", 77000))

    def test_package_allowlist_and_encrypted_payload(self):
        with patch.dict(os.environ, {"PUSHBULLET_ALLOWED_PACKAGES": "com.allowed.bank"}, clear=False):
            self.assertIsNone(parse_deposit_notice(mirror("입금", "1,000원 입금 홍길동")))
            self.assertIsNotNone(parse_deposit_notice(mirror("입금", "1,000원 입금 홍길동", package="com.allowed.bank")))
        encrypted = mirror("입금", "1,000원 입금 홍길동")
        encrypted["encrypted"] = True
        self.assertIsNone(parse_deposit_notice(encrypted))

    def test_event_id_is_stable_and_name_normalization_is_exact(self):
        first = parse_deposit_notice(mirror("입금", "1,000원 입금 홍 길동"), pattern=r"(?P<amount>[0-9,]+)원 입금 (?P<name>.+)")
        second = parse_deposit_notice(mirror("입금", "1,000원 입금 홍 길동"), pattern=r"(?P<amount>[0-9,]+)원 입금 (?P<name>.+)")
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(normalize_depositor(" 홍 길동 "), normalize_depositor("홍길동"))

    def test_notification_update_with_same_android_identity_is_not_reprocessed(self):
        original = mirror("입금", "1,000원 입금 홍길동")
        updated = mirror("입금 확인", "1,000원 입금 홍길동 잔액이 변경되었습니다.")
        first = parse_deposit_notice(original)
        second = parse_deposit_notice(updated)
        self.assertEqual(first.event_id, second.event_id)


if __name__ == "__main__":
    unittest.main()
