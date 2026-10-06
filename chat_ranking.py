"""서버별 채팅 활동 순위와 Components V2 페이지 패널."""

import datetime as dt
import logging
import math
import random

import discord
from discord.ext import commands, tasks

from admin_roles import get_admin_role_ids, member_has_admin_role


LOG = logging.getLogger(__name__)
UTC = dt.timezone.utc
PAGE_SIZE = 5
CHAT_RANK_PAGE_PREFIX = 'chat_rank_page:'


def as_datetime(value):
    if isinstance(value, dt.datetime):
        return value
    return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))


async def initialize_chat_ranking_schema(conn):
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS chat_rank_settings (
            guild_id BIGINT PRIMARY KEY,
            channel_id BIGINT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            configured_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS chat_rank_scores (
            guild_id BIGINT NOT NULL,
            channel_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            score BIGINT NOT NULL DEFAULT 0 CHECK (score >= 0),
            last_counted_at TIMESTAMPTZ NOT NULL,
            next_check_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (guild_id, channel_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS chat_rank_order
            ON chat_rank_scores (guild_id, channel_id, score DESC, user_id);
        CREATE TABLE IF NOT EXISTS chat_rank_panels (
            guild_id BIGINT NOT NULL,
            channel_id BIGINT NOT NULL,
            message_id BIGINT NOT NULL,
            page INTEGER NOT NULL DEFAULT 0 CHECK (page >= 0),
            PRIMARY KEY (guild_id, message_id)
        );
    ''')


async def configure_rank_channel(conn, guild_id: int, channel_id: int, configured_at: dt.datetime):
    await conn.execute('''
        INSERT INTO chat_rank_settings (guild_id, channel_id, enabled, configured_at)
        VALUES ($1, $2, TRUE, $3)
        ON CONFLICT (guild_id) DO UPDATE
        SET channel_id = EXCLUDED.channel_id,
            enabled = TRUE,
            configured_at = EXCLUDED.configured_at
    ''', guild_id, channel_id, configured_at)


async def record_rank_activity(
    conn,
    guild_id: int,
    channel_id: int,
    user_id: int,
    sent_at: dt.datetime,
    delay_seconds: int,
):
    """첫 활동을 1회 기록하고 이후에는 무작위 1~3분 간격으로만 기록합니다."""
    if not 60 <= delay_seconds <= 180:
        raise ValueError('채팅 순위 확인 간격은 60~180초여야 합니다.')
    async with conn.transaction():
        setting = await conn.fetchrow(
            'SELECT channel_id, enabled FROM chat_rank_settings WHERE guild_id = $1 FOR SHARE',
            guild_id,
        )
        if not setting or not setting['enabled'] or int(setting['channel_id']) != channel_id:
            return False

        current = await conn.fetchrow('''
            SELECT score, next_check_at FROM chat_rank_scores
            WHERE guild_id = $1 AND channel_id = $2 AND user_id = $3
            FOR UPDATE
        ''', guild_id, channel_id, user_id)
        next_check = sent_at + dt.timedelta(seconds=delay_seconds)
        if current is None:
            await conn.execute('''
                INSERT INTO chat_rank_scores
                    (guild_id, channel_id, user_id, score, last_counted_at, next_check_at)
                VALUES ($1, $2, $3, 1, $4, $5)
            ''', guild_id, channel_id, user_id, sent_at, next_check)
            return True
        if sent_at < as_datetime(current['next_check_at']):
            return False
        await conn.execute('''
            UPDATE chat_rank_scores
            SET score = score + 1, last_counted_at = $4, next_check_at = $5
            WHERE guild_id = $1 AND channel_id = $2 AND user_id = $3
        ''', guild_id, channel_id, user_id, sent_at, next_check)
        return True


def rank_emoji(rank: int) -> str:
    special = {
        1: '<a:24171stplace:1553325685827833926>',
        2: '<:63082nd:1553325689049055272>',
        3: '<:48023rd:1553325687337783346>',
        4: '4️⃣',
        5: '5️⃣',
    }
    return special.get(rank, '🏅')


async def remove_ranked_admins(conn, guild, guild_id: int):
    role_ids = await get_admin_role_ids(conn, guild_id)
    if not role_ids:
        return
    excluded = {
        member.id
        for role_id in role_ids
        for role in [guild.get_role(role_id)]
        if role is not None
        for member in role.members
        if not member.bot
    }
    if excluded:
        await conn.execute(
            'DELETE FROM chat_rank_scores WHERE guild_id = $1 AND user_id = ANY($2::bigint[])',
            guild_id,
            sorted(excluded),
        )


async def build_rank_panel(bot, guild, requested_page: int):
    async with bot.db_pool.acquire() as conn:
        await remove_ranked_admins(conn, guild, guild.id)
        setting = await conn.fetchrow(
            'SELECT channel_id FROM chat_rank_settings WHERE guild_id = $1 AND enabled',
            guild.id,
        )
        if not setting:
            return None, 0
        channel_id = int(setting['channel_id'])
        total = int(await conn.fetchval('''
            SELECT COUNT(*) FROM chat_rank_scores
            WHERE guild_id = $1 AND channel_id = $2 AND score > 0
        ''', guild.id, channel_id) or 0)
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(max(0, requested_page), total_pages - 1)
        rows = await conn.fetch('''
            SELECT user_id, score FROM chat_rank_scores
            WHERE guild_id = $1 AND channel_id = $2 AND score > 0
            ORDER BY score DESC, last_counted_at ASC, user_id ASC
            LIMIT $3 OFFSET $4
        ''', guild.id, channel_id, PAGE_SIZE, page * PAGE_SIZE)

    if page == 0:
        content = '## <a:267042fire:1553325691582292049> VAPE SP0T 채팅 순위 TOP 5\n\n'
    else:
        content = '## 💬 VAPE SP0T 채팅 순위\n\n'

    if rows:
        lines = []
        for index, row in enumerate(rows, start=page * PAGE_SIZE + 1):
            lines.append(
                f'{rank_emoji(index)} **{index}위 :** <@{row["user_id"]}>\n'
                f'> 체크 횟수 : `{int(row["score"]):,}회`'
            )
        content += '\n\n'.join(lines)
    else:
        content += '아직 집계된 채팅 활동이 없습니다.'

    content += (
        f'\n\n-# 📍 집계 채널 : <#{channel_id}>'
        f'\n-# 📄 페이지 : {page + 1}/{total_pages} · 5명씩 표시'
        '\n-# 1인당 무작위 1~3분 간격으로 채팅 활동을 체크합니다.'
    )
    buttons = [
        {
            'type': 2,
            'style': 2,
            'label': '이전',
            'emoji': {'name': '◀️'},
            'custom_id': f'{CHAT_RANK_PAGE_PREFIX}{guild.id}:{max(0, page - 1)}',
            'disabled': page == 0,
        },
        {
            'type': 2,
            'style': 2,
            'label': f'{page + 1} / {total_pages}',
            'custom_id': f'chat_rank_page_label:{guild.id}:{page}',
            'disabled': True,
        },
        {
            'type': 2,
            'style': 2,
            'label': '다음',
            'emoji': {'name': '▶️'},
            'custom_id': f'{CHAT_RANK_PAGE_PREFIX}{guild.id}:{min(total_pages - 1, page + 1)}',
            'disabled': page >= total_pages - 1,
        },
    ]
    payload = {
        'flags': 1 << 15,
        'allowed_mentions': {'parse': []},
        'components': [
            {
                'type': 17,
                'accent_color': 0x32CD32,
                'components': [
                    {'type': 10, 'content': content},
                    {'type': 1, 'components': buttons},
                ],
            }
        ],
    }
    return payload, page


async def handle_rank_page(interaction, bot):
    custom_id = (interaction.data or {}).get('custom_id', '')
    parts = custom_id.split(':')
    if len(parts) != 3 or parts[0] != 'chat_rank_page' or not parts[1].isdigit() or not parts[2].isdigit():
        await interaction.response.send_message('❌ 올바르지 않은 순위 페이지입니다.', ephemeral=True)
        return
    guild_id, requested_page = int(parts[1]), int(parts[2])
    if interaction.guild is None or interaction.guild.id != guild_id or bot.db_pool is None:
        await interaction.response.send_message('❌ 이 서버의 순위 패널이 아닙니다.', ephemeral=True)
        return
    message_id = getattr(getattr(interaction, 'message', None), 'id', None)
    async with bot.db_pool.acquire() as conn:
        exists = await conn.fetchval('''
            SELECT 1 FROM chat_rank_panels
            WHERE guild_id = $1 AND channel_id = $2 AND message_id = $3
        ''', guild_id, interaction.channel_id, message_id)
    if not exists:
        await interaction.response.send_message('❌ 등록되지 않은 순위 패널입니다.', ephemeral=True)
        return

    await interaction.response.defer()
    payload, actual_page = await build_rank_panel(bot, interaction.guild, requested_page)
    if payload is None:
        await interaction.followup.send('❌ `/채팅순위`로 집계 채널을 먼저 설정해 주세요.', ephemeral=True)
        return
    await interaction.client.http.request(
        discord.http.Route('PATCH', f'/channels/{interaction.channel_id}/messages/{message_id}'),
        json=payload,
    )
    async with bot.db_pool.acquire() as conn:
        await conn.execute('''
            UPDATE chat_rank_panels SET page = $4
            WHERE guild_id = $1 AND channel_id = $2 AND message_id = $3
        ''', guild_id, interaction.channel_id, message_id, actual_page)


class ChatRankingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.refresh_panels.start()

    async def cog_unload(self):
        self.refresh_panels.cancel()

    @commands.Cog.listener()
    async def on_message(self, message):
        if (
            not message.guild
            or message.author.bot
            or message.webhook_id
            or message.type not in (discord.MessageType.default, discord.MessageType.reply)
            or not (message.content.strip() or message.attachments or message.stickers)
            or self.bot.db_pool is None
        ):
            return
        now = dt.datetime.now(UTC)
        if abs((now - message.created_at).total_seconds()) > 120:
            return
        try:
            async with self.bot.db_pool.acquire() as conn:
                role_ids = await get_admin_role_ids(conn, message.guild.id)
                if member_has_admin_role(message.author, role_ids):
                    return
                await record_rank_activity(
                    conn,
                    message.guild.id,
                    message.channel.id,
                    message.author.id,
                    message.created_at,
                    random.randint(60, 180),
                )
        except Exception:
            LOG.exception('채팅 순위 기록 실패: guild=%s user=%s', message.guild.id, message.author.id)

    @tasks.loop(minutes=1)
    async def refresh_panels(self):
        if self.bot.db_pool is None:
            return
        async with self.bot.db_pool.acquire() as conn:
            panels = await conn.fetch('SELECT * FROM chat_rank_panels ORDER BY guild_id, message_id')
        for panel in panels:
            guild = self.bot.get_guild(panel['guild_id'])
            if guild is None:
                continue
            try:
                payload, page = await build_rank_panel(self.bot, guild, int(panel['page']))
                if payload is None:
                    continue
                await self.bot.http.request(
                    discord.http.Route(
                        'PATCH',
                        f'/channels/{panel["channel_id"]}/messages/{panel["message_id"]}',
                    ),
                    json=payload,
                )
                if page != panel['page']:
                    async with self.bot.db_pool.acquire() as conn:
                        await conn.execute('''
                            UPDATE chat_rank_panels SET page = $3
                            WHERE guild_id = $1 AND message_id = $2
                        ''', panel['guild_id'], panel['message_id'], page)
            except discord.NotFound:
                async with self.bot.db_pool.acquire() as conn:
                    await conn.execute(
                        'DELETE FROM chat_rank_panels WHERE guild_id = $1 AND message_id = $2',
                        panel['guild_id'],
                        panel['message_id'],
                    )
            except discord.HTTPException:
                LOG.warning('채팅 순위 패널 갱신 실패: guild=%s message=%s', panel['guild_id'], panel['message_id'])

    @refresh_panels.before_loop
    async def before_refresh_panels(self):
        await self.bot.wait_until_ready()


def register_chat_ranking_commands(bot):
    @bot.tree.command(name='채팅순위', description='채팅 활동 순위를 집계할 채널을 설정합니다. (관리자 전용)')
    @discord.app_commands.describe(채널='채팅 순위를 집계할 채널')
    async def configure_chat_ranking(interaction: discord.Interaction, 채널: discord.TextChannel):
        if bot.db_pool is None:
            await interaction.response.send_message('❌ 데이터베이스가 연결되지 않았습니다.', ephemeral=True)
            return
        if 채널.guild.id != interaction.guild.id:
            await interaction.response.send_message('❌ 이 서버의 채널을 선택해 주세요.', ephemeral=True)
            return
        permissions = 채널.permissions_for(interaction.guild.me)
        if not permissions.view_channel:
            await interaction.response.send_message('❌ 봇이 해당 채널을 볼 수 있도록 권한을 설정해 주세요.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        configured_at = getattr(interaction, 'created_at', dt.datetime.now(UTC))
        async with bot.db_pool.acquire() as conn:
            await configure_rank_channel(conn, interaction.guild.id, 채널.id, configured_at)
        await interaction.followup.send(
            f'✅ {채널.mention}의 채팅 활동으로 순위를 집계합니다.\n'
            '첫 활동을 기록한 뒤 유저별 무작위 1~3분 간격으로 다음 활동을 체크합니다.\n'
            '도배해도 확인 간격 안에서는 순위 점수가 추가되지 않습니다.',
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.tree.command(name='순위랭킹패널', description='페이지 방식의 채팅 순위 패널을 생성합니다. (관리자 전용)')
    async def create_chat_rank_panel(interaction: discord.Interaction):
        if bot.db_pool is None:
            await interaction.response.send_message('❌ 데이터베이스가 연결되지 않았습니다.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        payload, page = await build_rank_panel(bot, interaction.guild, 0)
        if payload is None:
            await interaction.followup.send('❌ `/채팅순위`로 집계 채널을 먼저 설정해 주세요.', ephemeral=True)
            return
        response = await interaction.client.http.request(
            discord.http.Route('POST', f'/channels/{interaction.channel_id}/messages'),
            json=payload,
        )
        if not response or not response.get('id'):
            await interaction.followup.send('❌ 순위 패널 메시지 ID를 확인하지 못했습니다.', ephemeral=True)
            return
        async with bot.db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO chat_rank_panels (guild_id, channel_id, message_id, page)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (guild_id, message_id) DO UPDATE SET page = EXCLUDED.page
            ''', interaction.guild.id, interaction.channel_id, int(response['id']), page)
        await interaction.followup.send('✅ 채팅 순위 랭킹 패널을 생성했습니다.', ephemeral=True)

    return configure_chat_ranking, create_chat_rank_panel
