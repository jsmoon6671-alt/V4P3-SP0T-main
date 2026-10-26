"""봇을 실행하지 않고 구매패널의 저장 금액과 Discord 안내를 검증합니다."""

import ast
from pathlib import Path
import random
import string
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from loyalty_points import PointsError, parse_amount, payment_summary


def load_purchase_panel(pool):
    # bot.py를 import하면 봇과 외부 연결이 시작되므로 해당 명령어만 로드합니다.
    tree = ast.parse((Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "send_purchase_panel")
    function.decorator_list = []
    namespace = {
        "bot": SimpleNamespace(db_pool=pool),
        "discord": SimpleNamespace(Interaction=object, Member=object,
                                   http=SimpleNamespace(Route=lambda *args: args)),
        "parse_amount": parse_amount,
        "PointsError": PointsError,
        "random": random,
        "string": string,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "bot.py", "exec"), namespace)
    return namespace["send_purchase_panel"]


class PurchasePanelShippingTests(unittest.IsolatedAsyncioTestCase):
    async def test_amount_saved_and_displayed_at_shipping_boundary(self):
        for amount, shipping, total in (
            (1, 3000, 3001),
            (30000, 3000, 33000),
            (34999, 3000, 37999),
            (35000, 0, 35000),
            (35001, 0, 35001),
            (50000, 0, 50000),
        ):
            with self.subTest(amount=amount):
                conn = SimpleNamespace(execute=AsyncMock())
                pool = MagicMock()
                pool.acquire.return_value.__aenter__.return_value = conn
                interaction = SimpleNamespace(
                    guild=SimpleNamespace(id=10), channel=SimpleNamespace(id=20),
                    id=30, token="test", response=SimpleNamespace(send_message=AsyncMock()),
                    client=SimpleNamespace(http=SimpleNamespace(request=AsyncMock())),
                )
                buyer = SimpleNamespace(id=40, mention="<@40>")
                await load_purchase_panel(pool)(interaction, buyer, f"{amount:,}원", "상품 A, 상품 B", "2", False)

                saved_amount = conn.execute.call_args.args[7]
                self.assertEqual(saved_amount, f"{total:,}원")
                self.assertFalse(conn.execute.call_args.args[9])
                payload = interaction.client.http.request.call_args.kwargs["json"]["data"]
                content = payload["components"][0]["components"][0]["content"]
                self.assertIn(f"`{amount:,}원`", content)
                self.assertIn(f"`{shipping:,}원`", content)
                self.assertIn(f"`{total:,}원`", content)
                self.assertIn("35,000원 미만이라 배송비 3,000원이 추가" if shipping
                              else "35,000원 이상이라 배송비가 무료", content)
                # 결제 안내는 저장된 합계에서 포인트만 차감해야 합니다.
                summary = payment_summary({"amount": saved_amount, "points_used": 500})
                self.assertIn(f"실제 입금금액: `{total - 500:,}원`", summary)

    async def test_invalid_product_amount_does_not_create_order(self):
        conn = SimpleNamespace(execute=AsyncMock())
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await load_purchase_panel(pool)(interaction, SimpleNamespace(), "잘못된 금액", "상품", "1")
        interaction.response.send_message.assert_awaited_once()
        pool.acquire.assert_not_called()
        conn.execute.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
