import ast
import asyncio
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord
from discord import app_commands


def review_functions(pool):
    names = {"create_v2_payload", "send_admin_review_log", "write_review", "set_admin_review_log_channel"}
    tree = ast.parse((Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name in names]
    for node in nodes:
        node.decorator_list = []
    namespace = dict(
        discord=discord, app_commands=app_commands, asyncio=asyncio, io=io,
        json=json, logging=logging, bot=SimpleNamespace(db_pool=pool),
        award_review_points=AsyncMock(return_value=(100, 500)),
        watermark_review_image=lambda image: image,
        ReviewImageError=ValueError,
        update_setting=AsyncMock(),
    )
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "bot.py", "exec"), namespace)
    return namespace


class AdminReviewLogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = dict(buyer_role_id=11, review_channel_id=22,
                             admin_review_log_channel_id=33, anonymous_role_id=44,
                             review_auto_message=None)
        self.conn = SimpleNamespace(fetchrow=AsyncMock(return_value=self.settings))
        self.pool = MagicMock()
        self.pool.acquire.return_value.__aenter__.return_value = self.conn
        self.functions = review_functions(self.pool)
        self.role = object()
        self.http = SimpleNamespace(request=AsyncMock(return_value={"id": "66", "attachments": []}))
        self.interaction = SimpleNamespace(
            id=77, guild=SimpleNamespace(id=88, get_role=lambda _: self.role,
                                        get_channel=lambda _: SimpleNamespace(id=22)),
            user=SimpleNamespace(id=99, display_name="구매자닉네임", roles=[self.role]),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            client=SimpleNamespace(http=self.http),
        )

    async def write(self, photo=None):
        await self.functions['write_review'](
            self.interaction, SimpleNamespace(value=5), "정말 만족스러운 상품입니다.\\n다음에 또 구매할게요!", photo,
        )

    async def test_private_log_identifies_author_and_public_review_stays_anonymous(self):
        await self.write()
        public, private = self.http.request.call_args_list
        self.assertTrue(public.args[0].url.endswith('/channels/22/messages'))
        self.assertTrue(private.args[0].url.endswith('/channels/33/messages'))
        public_text = str(public.kwargs['json'])
        private_payload = private.kwargs['json']
        private_text = str(private_payload)
        self.assertIn("<@&44>", public_text)
        self.assertNotIn("<@99>", public_text)
        for expected in ("<@99>", "구매자닉네임", "99", "⭐⭐⭐⭐⭐",
                         "정말 만족스러운 상품입니다.", "다음에 또 구매할게요!",
                         "https://discord.com/channels/88/22/66"):
            self.assertIn(expected, private_text)
        self.assertEqual(private_payload['allowed_mentions'], {"parse": []})
        self.functions['award_review_points'].assert_awaited_once()

    async def test_photo_upload_result_is_also_visible_in_admin_log(self):
        self.http.request.side_effect = [
            {"id": "66", "attachments": [{"url": "https://cdn.discordapp.com/test/review.jpg"}]}, {},
        ]
        photo = SimpleNamespace(content_type="image/jpeg", read=AsyncMock(return_value=b"test-image"))
        await self.write(photo)
        self.assertIn('files', self.http.request.call_args_list[0].kwargs)
        payload = self.http.request.call_args_list[1].kwargs['json']
        gallery = payload['components'][0]['components'][-1]
        self.assertEqual(gallery['type'], 12)
        self.assertEqual(gallery['items'][0]['media']['url'], "https://cdn.discordapp.com/test/review.jpg")

    async def test_unconfigured_log_channel_sends_only_public_review(self):
        self.settings['admin_review_log_channel_id'] = None
        await self.write()
        self.http.request.assert_awaited_once()
        self.functions['award_review_points'].assert_awaited_once()

    async def test_log_delivery_failure_does_not_fail_review_or_points(self):
        self.http.request.side_effect = [{"id": "66"}, RuntimeError("channel unavailable")]
        with self.assertLogs(level="ERROR"):
            await self.write()
        self.functions['award_review_points'].assert_awaited_once()
        self.assertIn("후기 등록이 정상적으로 완료", self.interaction.followup.send.call_args.args[0])

    async def test_failed_public_review_does_not_send_log_or_award_points(self):
        self.http.request.side_effect = RuntimeError("public upload failed")
        with self.assertRaises(RuntimeError):
            await self.write()
        self.http.request.assert_awaited_once()
        self.functions['award_review_points'].assert_not_awaited()

    async def test_setting_command_saves_separate_channel(self):
        channel = SimpleNamespace(id=33, mention="<#33>")
        await self.functions['set_admin_review_log_channel'](self.interaction, channel)
        args = self.functions['update_setting'].call_args.args
        self.assertEqual(args[:3], (self.interaction, "admin_review_log_channel_id", 33))


if __name__ == "__main__":
    unittest.main()
