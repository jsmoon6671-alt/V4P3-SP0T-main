"""채팅 활동 보상·일일 랜덤박스. 메시지 내용은 저장하지 않습니다."""

import datetime as dt
import logging
import random

import discord
from discord.ext import commands, tasks

from loyalty_points import get_balance

LOG = logging.getLogger(__name__)
KST = dt.timezone(dt.timedelta(hours=9))
UTC = dt.timezone.utc
REWARDS_ID = "chat_points_rewards"
BOX_PREFIX = "chat_points_open_box:"


def clock_parts(now):
    now = now.astimezone(UTC)
    return now.replace(second=0, microsecond=0), now.astimezone(KST).date()


async def initialize_chat_points_schema(conn):
    async with conn.transaction():
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS chat_point_settings (
                guild_id BIGINT PRIMARY KEY,
                channel_id BIGINT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                next_announcement TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS chat_activity_messages (
                guild_id BIGINT NOT NULL, message_id BIGINT NOT NULL,
                sent_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (guild_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS chat_activity_minutes (
                guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
                minute TIMESTAMPTZ NOT NULL, channel_id BIGINT NOT NULL,
                rewarded BOOLEAN NOT NULL DEFAULT FALSE,
                PRIMARY KEY (guild_id, user_id, minute)
            );
            CREATE INDEX IF NOT EXISTS chat_minutes_pending
                ON chat_activity_minutes (guild_id, minute) WHERE NOT rewarded;
            CREATE TABLE IF NOT EXISTS chat_activity_daily (
                guild_id BIGINT NOT NULL, activity_day DATE NOT NULL,
                user_id BIGINT NOT NULL, channel_id BIGINT NOT NULL,
                message_count BIGINT NOT NULL DEFAULT 1,
                first_message_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (guild_id, activity_day, user_id)
            );
            CREATE TABLE IF NOT EXISTS chat_point_rewards (
                guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
                minute TIMESTAMPTZ NOT NULL, channel_id BIGINT NOT NULL,
                points INTEGER NOT NULL CHECK (points BETWEEN 10 AND 50),
                notified BOOLEAN NOT NULL DEFAULT FALSE,
                PRIMARY KEY (guild_id, user_id, minute)
            );
            CREATE TABLE IF NOT EXISTS chat_random_boxes (
                guild_id BIGINT NOT NULL, activity_day DATE NOT NULL,
                user_id BIGINT NOT NULL, channel_id BIGINT NOT NULL,
                points INTEGER CHECK (points BETWEEN 100 AND 300),
                opened_at TIMESTAMPTZ, notified BOOLEAN NOT NULL DEFAULT FALSE,
                PRIMARY KEY (guild_id, activity_day)
            );
        ''')


async def configure_channel(conn, guild_id, channel_id, enabled, now):
    await conn.execute('''
        INSERT INTO chat_point_settings (guild_id, channel_id, enabled, next_announcement)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (guild_id) DO UPDATE SET channel_id = EXCLUDED.channel_id,
            enabled = EXCLUDED.enabled, next_announcement = EXCLUDED.next_announcement
    ''', guild_id, channel_id, enabled, now + dt.timedelta(minutes=5))


async def record_activity(conn, guild_id, channel_id, user_id, message_id, sent_at):
    minute, day = clock_parts(sent_at)
    async with conn.transaction():
        # 채널 변경 및 정산과 동시에 처리되어도 같은 서버의 기록을 순서대로 반영합니다.
        setting = await conn.fetchrow('SELECT * FROM chat_point_settings WHERE guild_id = $1 FOR UPDATE', guild_id)
        if not setting or not setting['enabled'] or setting['channel_id'] != channel_id:
            return False
        inserted = await conn.fetchval('''
            INSERT INTO chat_activity_messages (guild_id, message_id, sent_at) VALUES ($1, $2, $3)
            ON CONFLICT DO NOTHING RETURNING message_id
        ''', guild_id, message_id, sent_at)
        if inserted is None:
            return False
        await conn.execute('''
            INSERT INTO chat_activity_minutes (guild_id, user_id, minute, channel_id)
            VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING
        ''', guild_id, user_id, minute, channel_id)
        await conn.execute('''
            INSERT INTO chat_activity_daily
                (guild_id, activity_day, user_id, channel_id, first_message_at)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (guild_id, activity_day, user_id) DO UPDATE
            SET message_count = chat_activity_daily.message_count + 1,
                channel_id = EXCLUDED.channel_id,
                first_message_at = LEAST(chat_activity_daily.first_message_at, EXCLUDED.first_message_at)
        ''', guild_id, day, user_id, channel_id, sent_at)
        return True


async def _credit(conn, guild_id, user_id, points):
    await conn.execute('''
        INSERT INTO point_balances (guild_id, user_id, balance) VALUES ($1, $2, $3)
        ON CONFLICT (guild_id, user_id) DO UPDATE
        SET balance = point_balances.balance + EXCLUDED.balance
    ''', guild_id, user_id, points)


async def settle_activity(conn, guild_id, now):
    minute, _ = clock_parts(now)
    # 자정 직전 메시지가 지연 도착할 수 있어 2분의 기록 시간을 둡니다.
    closed_before = (now - dt.timedelta(minutes=2)).astimezone(KST).date()
    async with conn.transaction():
        setting = await conn.fetchrow('SELECT * FROM chat_point_settings WHERE guild_id = $1 FOR UPDATE', guild_id)
        if not setting:
            return
        pending = await conn.fetch('''
            SELECT * FROM chat_activity_minutes WHERE guild_id = $1 AND minute < $2 AND NOT rewarded
            ORDER BY minute, user_id LIMIT 500
        ''', guild_id, minute)
        for row in pending:
            reward = await conn.fetchval('''
                INSERT INTO chat_point_rewards (guild_id, user_id, minute, channel_id, points)
                VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING RETURNING points
            ''', guild_id, row['user_id'], row['minute'], row['channel_id'], random.randint(10, 50))
            if reward is not None:
                await _credit(conn, guild_id, row['user_id'], reward)
            await conn.execute('''
                UPDATE chat_activity_minutes SET rewarded = TRUE
                WHERE guild_id = $1 AND user_id = $2 AND minute = $3
            ''', guild_id, row['user_id'], row['minute'])
        # 하루 전체 채팅 수가 가장 많은 한 명. 동률이면 먼저 활동한 유저가 받습니다.
        winners = await conn.fetch('''
            SELECT DISTINCT ON (d.activity_day) d.* FROM chat_activity_daily d
            WHERE d.guild_id = $1 AND d.activity_day < $2
            AND NOT EXISTS (SELECT 1 FROM chat_random_boxes b
                WHERE b.guild_id = d.guild_id AND b.activity_day = d.activity_day)
            ORDER BY d.activity_day, d.message_count DESC, d.first_message_at, d.user_id
        ''', guild_id, closed_before)
        for row in winners:
            await conn.execute('''
                INSERT INTO chat_random_boxes (guild_id, activity_day, user_id, channel_id)
                VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING
            ''', guild_id, row['activity_day'], row['user_id'], row['channel_id'])
        await conn.execute('DELETE FROM chat_activity_messages WHERE guild_id = $1 AND sent_at < $2',
                           guild_id, now - dt.timedelta(days=3))
        await conn.execute('DELETE FROM chat_activity_minutes WHERE guild_id = $1 AND rewarded AND minute < $2',
                           guild_id, now - dt.timedelta(days=30))


async def open_box(conn, guild_id, user_id, now):
    async with conn.transaction():
        box = await conn.fetchrow('''
            SELECT * FROM chat_random_boxes WHERE guild_id = $1 AND user_id = $2 AND points IS NULL
            ORDER BY activity_day LIMIT 1 FOR UPDATE
        ''', guild_id, user_id)
        if box is None:
            return None
        points = random.randint(100, 300)
        await _credit(conn, guild_id, user_id, points)
        await conn.execute('''
            UPDATE chat_random_boxes SET points = $3, opened_at = $4
            WHERE guild_id = $1 AND activity_day = $2
        ''', guild_id, box['activity_day'], points, now)
        return points, await get_balance(conn, guild_id, user_id)


def box_button(guild_id):
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="랜덤박스 열기", emoji="🎁", style=discord.ButtonStyle.success,
                                   custom_id=BOX_PREFIX + str(guild_id)))
    return view


def announcement_payload():
    return {"flags": 1 << 15, "allowed_mentions": {"parse": ["everyone"]}, "components": [
        {"type": 10, "content": "@here"},
        {"type": 17, "accent_color": 0x32CD32, "components": [
            {"type": 10, "content": "## 💬 채팅 활동 포인트\n이 채널에서 채팅하면 **활동한 1분마다 랜덤 10P~50P**가 적립됩니다.\n같은 1분에 여러 메시지를 보내도 채팅 포인트는 한 번만 지급됩니다."},
            {"type": 14, "divider": True, "spacing": 1},
            {"type": 10, "content": "🎁 매일 가장 많이 채팅한 **1명**에게 랜덤박스를 드립니다!\n한국 시간 자정에 전날 활동을 정산하며, 박스를 열면 **100P~300P**를 받을 수 있습니다.\n지급 알림은 DM으로 전송됩니다. 아래에서 본인만 보이는 보상 내역을 확인하세요."},
            {"type": 1, "components": [{"type": 2, "style": 1, "label": "내 보상 확인", "custom_id": REWARDS_ID}]}]}]}


async def handle_reward_interaction(interaction, pool):
    if pool is None:
        await interaction.response.send_message("데이터베이스가 연결되지 않았습니다.", ephemeral=True)
        return
    custom_id = (interaction.data or {}).get('custom_id', '')
    if custom_id.startswith(BOX_PREFIX):
        suffix = custom_id[len(BOX_PREFIX):]
        if not suffix.isdigit() or not 0 < int(suffix) <= 9_223_372_036_854_775_807:
            await interaction.response.send_message("유효하지 않은 랜덤박스입니다.", ephemeral=True)
            return
        guild_id = int(suffix)
        if interaction.guild and interaction.guild.id != guild_id:
            await interaction.response.send_message("이 서버의 랜덤박스가 아닙니다.", ephemeral=True)
            return
    else:
        if not interaction.guild:
            await interaction.response.send_message("서버에서 보상 내역을 확인해 주세요.", ephemeral=True)
            return
        guild_id = interaction.guild.id
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with pool.acquire() as conn:
        if custom_id.startswith(BOX_PREFIX):
            result = await open_box(conn, guild_id, interaction.user.id, dt.datetime.now(UTC))
            text = (f"🎁 랜덤박스에서 **{result[0]:,}P**를 획득했습니다!\n현재 포인트: **{result[1]:,}P**"
                    if result else "열 수 있는 랜덤박스가 없습니다. 이미 연 박스는 다시 지급되지 않습니다.")
            await interaction.followup.send(text, ephemeral=True)
            return
        _, today = clock_parts(dt.datetime.now(UTC))
        total = await conn.fetchval('''
            SELECT COALESCE(SUM(points), 0) FROM chat_point_rewards
            WHERE guild_id = $1 AND user_id = $2 AND (minute AT TIME ZONE 'Asia/Seoul')::date = $3
        ''', guild_id, interaction.user.id, today)
        recent = await conn.fetch('''
            SELECT points, minute FROM chat_point_rewards WHERE guild_id = $1 AND user_id = $2
            ORDER BY minute DESC LIMIT 5
        ''', guild_id, interaction.user.id)
        boxes = await conn.fetchval('''
            SELECT COUNT(*) FROM chat_random_boxes WHERE guild_id = $1 AND user_id = $2 AND points IS NULL
        ''', guild_id, interaction.user.id)
        balance = await get_balance(conn, guild_id, interaction.user.id)
    lines = ["## 💬 내 채팅 보상", f"오늘 적립한 채팅 포인트: **{total:,}P**", f"현재 포인트: **{balance:,}P**",
             f"받은 랜덤박스: **{boxes}개**"]
    if recent:
        lines.append("\n최근 지급 내역")
        for reward in recent:
            timestamp = dt.datetime.fromisoformat(str(reward['minute']).replace('Z', '+00:00')).astimezone(KST)
            lines.append(f"- {timestamp:%m/%d %H:%M}: 채팅 활동으로 **{reward['points']}P** 지급")
    await interaction.followup.send("\n".join(lines), ephemeral=True,
                                    view=box_button(guild_id) if boxes else None)


class ChatPointsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.check_activity.start()

    async def cog_unload(self):
        self.check_activity.cancel()

    @commands.Cog.listener()
    async def on_message(self, message):
        if (not message.guild or message.author.bot or message.webhook_id
                or message.type not in (discord.MessageType.default, discord.MessageType.reply)
                or not (message.content.strip() or message.attachments or message.stickers)
                or self.bot.db_pool is None):
            return
        # 재연결 시 오래된 이벤트가 정산을 뒤늦게 바꾸지 않도록 제한합니다.
        now = dt.datetime.now(UTC)
        if abs((now - message.created_at).total_seconds()) > 120:
            return
        try:
            async with self.bot.db_pool.acquire() as conn:
                await record_activity(conn, message.guild.id, message.channel.id, message.author.id,
                                      message.id, message.created_at)
        except Exception:
            LOG.exception("채팅 활동 기록 실패: guild=%s", message.guild.id)

    @tasks.loop(seconds=60)
    async def check_activity(self):
        if self.bot.db_pool is None:
            return
        now = dt.datetime.now(UTC)
        try:
            async with self.bot.db_pool.acquire() as conn:
                settings = await conn.fetch('SELECT * FROM chat_point_settings')
        except Exception:
            LOG.exception("채팅 포인트 설정 조회 실패")
            return
        for setting in settings:
            try:
                async with self.bot.db_pool.acquire() as conn:
                    await settle_activity(conn, setting['guild_id'], now)
                await self.notify_rewards(setting['guild_id'])
                if setting['enabled']:
                    await self.send_announcement(setting['guild_id'], now)
            except Exception:
                LOG.exception("채팅 포인트 정산 실패: guild=%s", setting['guild_id'])

    @check_activity.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    async def notify_rewards(self, guild_id):
        # 보상은 먼저 적립됩니다. DM 실패는 잔액이나 박스 지급을 취소하지 않습니다.
        async with self.bot.db_pool.acquire() as conn:
            rewards = await conn.fetch('''
                UPDATE chat_point_rewards SET notified = TRUE WHERE (guild_id, user_id, minute) IN (
                    SELECT guild_id, user_id, minute FROM chat_point_rewards
                    WHERE guild_id = $1 AND NOT notified ORDER BY minute LIMIT 50 FOR UPDATE SKIP LOCKED
                ) RETURNING *
            ''', guild_id)
            boxes = await conn.fetch('''
                UPDATE chat_random_boxes SET notified = TRUE WHERE (guild_id, activity_day) IN (
                    SELECT guild_id, activity_day FROM chat_random_boxes
                    WHERE guild_id = $1 AND NOT notified ORDER BY activity_day LIMIT 50 FOR UPDATE SKIP LOCKED
                ) RETURNING *
            ''', guild_id)
        for kind, rows in [('reward', rewards), ('box', boxes)]:
            for row in rows:
                try:
                    user = self.bot.get_user(row['user_id']) or await self.bot.fetch_user(row['user_id'])
                    if kind == 'reward':
                        timestamp = dt.datetime.fromisoformat(str(row['minute']).replace('Z', '+00:00')).astimezone(KST)
                        text = (f"💬 채팅 활동을 해서 랜덤으로 **{row['points']}P**가 지급되었습니다!\n"
                                f"채널: <#{row['channel_id']}> · 활동 시간: {timestamp:%m/%d %H:%M}")
                        await user.send(text, allowed_mentions=discord.AllowedMentions.none())
                    else:
                        text = (f"🎁 {row['activity_day']} 최다 채팅 활동자로 선정되어 **랜덤박스 1개**가 지급되었습니다!\n"
                                "아래 버튼을 눌러 열면 **100P~300P**를 획득합니다.")
                        await user.send(text, view=box_button(guild_id), allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    LOG.info("채팅 보상 DM 전송 실패: guild=%s user=%s (채널의 내 보상 확인 이용)", guild_id, row['user_id'])

    async def send_announcement(self, guild_id, now):
        async with self.bot.db_pool.acquire() as conn:
            # 여러 프로세스나 재연결에서도 안내를 중복 예약하지 않습니다.
            channel_id = await conn.fetchval('''
                UPDATE chat_point_settings SET next_announcement = $2
                WHERE guild_id = $1 AND enabled AND next_announcement <= $3 RETURNING channel_id
            ''', guild_id, now + dt.timedelta(minutes=5), now)
        if channel_id is None:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                LOG.warning("채팅 안내 채널 접근 실패: guild=%s channel=%s", guild_id, channel_id)
                return
        await self.bot.http.request(discord.http.Route("POST", f"/channels/{channel.id}/messages"),
                                    json=announcement_payload())


async def configure_chat_points(interaction, channel, enabled):
    pool = interaction.client.db_pool
    if pool is None:
        await interaction.response.send_message("데이터베이스가 연결되지 않았습니다.", ephemeral=True)
        return
    channel = channel or interaction.channel
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != interaction.guild.id:
        await interaction.response.send_message("이 서버의 텍스트 채널을 선택해 주세요.", ephemeral=True)
        return
    permissions = channel.permissions_for(interaction.guild.me)
    if enabled and not (permissions.view_channel and permissions.send_messages):
        await interaction.response.send_message("봇이 해당 채널을 보고 메시지를 보낼 수 있도록 권한을 설정해 주세요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    async with pool.acquire() as conn:
        await configure_channel(conn, interaction.guild.id, channel.id, enabled, dt.datetime.now(UTC))
    if enabled:
        text = (f"✅ {channel.mention}에서 채팅 포인트를 지급합니다.\n"
                "활동한 1분마다 10P~50P, 한국 시간 자정에 전날 최다 채팅 활동자 1명에게 랜덤박스 지급.\n"
                "5분마다 @here 안내를 보내며, 알림은 DM·내 보상 확인으로 확인할 수 있습니다.")
        if not permissions.mention_everyone:
            text += "\n현재 봇에게 @everyone·@here 멘션 권한이 없어 @here 알림이 울리지 않습니다."
    else:
        text = "✅ 채팅 포인트 적립과 5분 간격 안내를 중지했습니다. 이미 기록된 활동과 보상은 정산합니다."
    await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
