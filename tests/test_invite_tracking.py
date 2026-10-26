import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from invite_tracking import InviteTracker


def invite(code="example", uses=0, inviter_id=123):
    return SimpleNamespace(code=code, uses=uses,
                           inviter=SimpleNamespace(id=inviter_id) if inviter_id else None)


class InviteTrackingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tracker = InviteTracker()
        self.guild = SimpleNamespace(id=1, invites=AsyncMock())

    async def test_single_used_link_identifies_inviter_once(self):
        self.guild.invites.side_effect = [[invite(uses=2)], [invite(uses=3)], [invite(uses=3)]]
        await self.tracker.prime(self.guild)
        self.assertEqual(await self.tracker.find_inviter(self.guild), 123)
        self.assertIsNone(await self.tracker.find_inviter(self.guild))

    async def test_ambiguous_or_unknown_inviter_is_omitted(self):
        for before, after in (
            ([invite()], [invite()]),
            ([], [invite(uses=1)]),
            ([invite()], []),
            ([invite()], [invite(uses=2)]),
            ([invite(inviter_id=None)], [invite(uses=1, inviter_id=None)]),
            ([invite("a"), invite("b")], [invite("a", 1), invite("b", 1)]),
        ):
            with self.subTest(before=before, after=after):
                self.tracker = InviteTracker()
                self.guild.invites.side_effect = [before, after]
                await self.tracker.prime(self.guild)
                self.assertIsNone(await self.tracker.find_inviter(self.guild))

    async def test_concurrent_join_does_not_reuse_invite_increment(self):
        self.guild.invites.side_effect = [[invite()], [invite(uses=1)], [invite(uses=1)]]
        await self.tracker.prime(self.guild)
        results = await asyncio.gather(self.tracker.find_inviter(self.guild), self.tracker.find_inviter(self.guild))
        self.assertEqual(results, [123, None])

    async def test_invites_created_after_start_are_tracked(self):
        self.guild.invites.side_effect = [[], [invite(uses=1)]]
        await self.tracker.prime(self.guild)
        created = invite()
        created.guild = self.guild
        await self.tracker.invite_created(created)
        self.assertEqual(await self.tracker.find_inviter(self.guild), 123)
        await self.tracker.invite_deleted(created)
        self.assertNotIn(created.code, self.tracker._invites[self.guild.id])

    async def test_api_failure_discards_stale_counts(self):
        error = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "missing permission")
        self.guild.invites.side_effect = [[invite()], error, [invite(uses=2)]]
        await self.tracker.prime(self.guild)
        with self.assertLogs(level="WARNING"):
            self.assertIsNone(await self.tracker.find_inviter(self.guild))
        self.assertIsNone(await self.tracker.find_inviter(self.guild))

    async def test_join_log_subtext_and_mention_only_when_inviter_is_known(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'bot.py').read_text(encoding='utf-8'))
        nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in {'create_v2_payload', 'on_member_join'}]
        for node in nodes:
            node.decorator_list = []
        for inviter_id in (123, None):
            with self.subTest(inviter_id=inviter_id):
                pool = MagicMock()
                pool.acquire.return_value.__aenter__.return_value = SimpleNamespace(fetchval=AsyncMock(return_value=55))
                http = SimpleNamespace(request=AsyncMock())
                tracker = SimpleNamespace(find_inviter=AsyncMock(return_value=inviter_id))
                namespace = dict(discord=discord, logging=logging, invite_tracker=tracker,
                                 bot=SimpleNamespace(db_pool=pool, http=http))
                exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), namespace)
                member = SimpleNamespace(id=99, mention='<@99>',
                                         guild=SimpleNamespace(id=1, member_count=20, icon=None))
                await namespace['on_member_join'](member)
                payload = http.request.call_args.kwargs['json']
                text = str(payload['components'])
                if inviter_id:
                    self.assertIn('-# 초대자: <@123>', text)
                    self.assertIn('123', payload['allowed_mentions']['users'])
                else:
                    self.assertNotIn('초대자', text)
                    self.assertEqual(payload['allowed_mentions']['users'], ['99'])


if __name__ == '__main__':
    unittest.main()
