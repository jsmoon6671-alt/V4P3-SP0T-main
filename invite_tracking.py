"""초대 링크 사용 횟수를 비교해 확인 가능한 입장의 초대자를 찾습니다."""

import asyncio
import logging

import discord


class InviteTracker:
    def __init__(self):
        self._invites = {}
        self._locks = {}

    def _lock(self, guild_id):
        return self._locks.setdefault(guild_id, asyncio.Lock())

    @staticmethod
    def _snapshot(invite):
        return (invite.uses or 0, invite.inviter.id if invite.inviter else None)

    async def _fetch(self, guild):
        try:
            return {invite.code: self._snapshot(invite) for invite in await guild.invites()}
        except discord.HTTPException:
            # 권한 누락이나 API 오류 뒤 오래된 횟수로 초대자를 추측하지 않습니다.
            self._invites.pop(guild.id, None)
            logging.warning("초대자 확인 불가: guild=%s, 서버 관리 권한과 API 상태를 확인해 주세요.", guild.id)
            return None

    async def prime(self, guild):
        async with self._lock(guild.id):
            current = await self._fetch(guild)
            if current is not None:
                self._invites[guild.id] = current

    async def find_inviter(self, guild):
        async with self._lock(guild.id):
            previous = self._invites.get(guild.id)
            current = await self._fetch(guild)
            if current is None:
                return None
            self._invites[guild.id] = current
            if previous is None:
                return None

            changes = [
                (uses - previous[code][0], inviter_id)
                for code, (uses, inviter_id) in current.items()
                if code in previous and uses > previous[code][0]
            ]
            # 여러 입장이나 링크가 동시에 증가하면 해당 멤버의 초대자를 단정하지 않습니다.
            if len(changes) == 1 and changes[0][0] == 1:
                return changes[0][1]
            return None

    async def invite_created(self, invite):
        if invite.guild is None:
            return
        async with self._lock(invite.guild.id):
            cached = self._invites.get(invite.guild.id)
            if cached is not None:
                cached.setdefault(invite.code, self._snapshot(invite))

    async def invite_deleted(self, invite):
        if invite.guild is None:
            return
        async with self._lock(invite.guild.id):
            self._invites.get(invite.guild.id, {}).pop(invite.code, None)

    def forget(self, guild_id):
        self._invites.pop(guild_id, None)
        self._locks.pop(guild_id, None)
