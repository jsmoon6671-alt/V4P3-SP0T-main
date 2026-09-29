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
            ALTER TABLE chat_point_settings ADD COLUMN IF NOT EXISTS reward_interval_minutes
                INTEGER NOT NULL DEFAULT 1 CHECK (reward_interval_minutes BETWEEN 1 AND 1440);
            ALTER TABLE chat_point_settings ADD COLUMN IF NOT EXISTS reward_anchor
                TIMESTAMPTZ NOT NULL DEFAULT date_trunc('minute', CURRENT_TIMESTAMP);
            ALTER TABLE chat_point_rewards ADD COLUMN IF NOT EXISTS reward_interval_minutes
                INTEGER NOT NULL DEFAULT 0;
            DO $$ BEGIN
                IF (SELECT cardinality(conkey) FROM pg_constraint
                    WHERE conrelid = 'chat_point_rewards'::regclass AND contype = 'p') = 3 THEN
                    ALTER TABLE chat_point_rewards DROP CONSTRAINT chat_point_rewards_pkey;
                    ALTER TABLE chat_point_rewards ADD PRIMARY KEY
                        (guild_id, user_id, minute, reward_interval_minutes);
                END IF;
            END $$;
            CREATE TABLE IF NOT EXISTS chat_reward_rounds (
                guild_id BIGINT NOT NULL, window_start TIMESTAMPTZ NOT NULL,
                interval_minutes INTEGER NOT NULL, user_id BIGINT NOT NULL,
                points INTEGER NOT NULL CHECK (points BETWEEN 10 AND 50),
                PRIMARY KEY (guild_id, window_start, interval_minutes)
            );
            ALTER TABLE chat_activity_messages ADD COLUMN IF NOT EXISTS user_id BIGINT;
            ALTER TABLE chat_activity_messages ADD COLUMN IF NOT EXISTS channel_id BIGINT;
            CREATE INDEX IF NOT EXISTS chat_message_candidates ON chat_activity_messages (guild_id, sent_at);
            ALTER TABLE chat_point_settings ADD COLUMN IF NOT EXISTS next_reward_at TIMESTAMPTZ;
            UPDATE chat_point_settings SET next_reward_at = reward_anchor
                + make_interval(mins => reward_interval_minutes) WHERE next_reward_at IS NULL;
            CREATE TABLE IF NOT EXISTS chat_reward_log_settings (
                guild_id BIGINT PRIMARY KEY, channel_id BIGINT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_reward_log_events (
                guild_id BIGINT NOT NULL, event_key TEXT NOT NULL,
                user_id BIGINT NOT NULL, kind TEXT NOT NULL,
                points INTEGER, activity_day DATE, source_channel_id BIGINT NOT NULL,
                occurred_at TIMESTAMPTZ NOT NULL, sent_at TIMESTAMPTZ,
                lease_until TIMESTAMPTZ, next_attempt TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (guild_id, event_key)
            );
        ''')


async def configure_channel(conn, guild_id, channel_id, enabled, now, interval_minutes=1):
    if not isinstance(interval_minutes, int) or not 1 <= interval_minutes <= 1440:
        raise ValueError("지급 주기는 1~1440분으로 설정해 주세요.")
    anchor = now.astimezone(UTC)
    await conn.execute('''
        INSERT INTO chat_point_settings
            (guild_id, channel_id, enabled, reward_interval_minutes, reward_anchor, next_reward_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (guild_id) DO UPDATE SET channel_id = EXCLUDED.channel_id,
            enabled = EXCLUDED.enabled,
            reward_interval_minutes = CASE WHEN EXCLUDED.enabled
                THEN EXCLUDED.reward_interval_minutes ELSE chat_point_settings.reward_interval_minutes END,
            reward_anchor = CASE WHEN EXCLUDED.enabled
                THEN EXCLUDED.reward_anchor ELSE chat_point_settings.reward_anchor END,
            next_reward_at = CASE WHEN EXCLUDED.enabled
                THEN EXCLUDED.next_reward_at ELSE chat_point_settings.next_reward_at END
    ''', guild_id, channel_id, enabled, interval_minutes, anchor,
         anchor + dt.timedelta(minutes=interval_minutes))


async def record_activity(conn, guild_id, channel_id, user_id, message_id, sent_at):
    minute, day = clock_parts(sent_at)
    async with conn.transaction():
        # 채널 변경 및 정산과 동시에 처리되어도 같은 서버의 기록을 순서대로 반영합니다.
        setting = await conn.fetchrow('SELECT * FROM chat_point_settings WHERE guild_id = $1 FOR UPDATE', guild_id)
        if not setting or not setting['enabled'] or setting['channel_id'] != channel_id:
            return False
        inserted = await conn.fetchval('''
            INSERT INTO chat_activity_messages (guild_id, message_id, sent_at, user_id, channel_id)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT DO NOTHING RETURNING message_id
        ''', guild_id, message_id, sent_at, user_id, channel_id)
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


async def queue_reward_log(conn, guild_id, event_key, user_id, kind, points, day, channel_id, now):
    await conn.execute('''
        INSERT INTO chat_reward_log_events
            (guild_id, event_key, user_id, kind, points, activity_day, source_channel_id, occurred_at, next_attempt)
        SELECT $1, $2, $3, $4, $5, $6, $7, $8, $8
        WHERE EXISTS (SELECT 1 FROM chat_reward_log_settings WHERE guild_id = $1)
        ON CONFLICT DO NOTHING
    ''', guild_id, event_key, user_id, kind, points, day, channel_id, now)


async def settle_activity(conn, guild_id, now, *, include_daily=True):
    # 자정 직전 메시지가 지연 도착할 수 있어 2분의 기록 시간을 둡니다.
    closed_before = (now - dt.timedelta(minutes=2)).astimezone(KST).date()
    async with conn.transaction():
        setting = await conn.fetchrow('SELECT * FROM chat_point_settings WHERE guild_id = $1 FOR UPDATE', guild_id)
        if not setting:
            return
        interval = setting['reward_interval_minutes']
        anchor = as_datetime(setting['reward_anchor'])
        duration = dt.timedelta(minutes=interval)
        next_reward = as_datetime(setting['next_reward_at'])
        start_at = next_reward - duration
        pending = await conn.fetch('''
            SELECT date_bin(make_interval(mins => $2::integer), sent_at, $3::timestamptz) AS window_start
            FROM chat_activity_messages WHERE guild_id = $1 AND user_id IS NOT NULL AND sent_at >= $5
            GROUP BY window_start
            HAVING date_bin(make_interval(mins => $2::integer), MIN(sent_at), $3::timestamptz)
                + make_interval(mins => $2::integer) <= $4
            ORDER BY window_start LIMIT 501
        ''', guild_id, interval, anchor, now, start_at)
        for window in pending[:500]:
            start = as_datetime(window['window_start'])
            end = start + dt.timedelta(minutes=interval)
            # 같은 유저의 메시지 수와 관계없이 주기마다 후보 목록에는 한 번만 포함합니다.
            candidates = await conn.fetch('''
                SELECT DISTINCT ON (user_id) user_id, channel_id FROM chat_activity_messages
                WHERE guild_id = $1 AND sent_at >= $2 AND sent_at < $3 AND user_id IS NOT NULL
                ORDER BY user_id, sent_at DESC
            ''', guild_id, start, end)
            row = random.choice(candidates)
            reward = await conn.fetchval('''
                INSERT INTO chat_reward_rounds (guild_id, window_start, interval_minutes, user_id, points)
                VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING RETURNING points
            ''', guild_id, start, interval, row['user_id'], random.randint(10, 50))
            if reward is not None:
                await conn.execute('''
                    INSERT INTO chat_point_rewards
                        (guild_id, user_id, minute, channel_id, points, reward_interval_minutes)
                    VALUES ($1, $2, $3, $4, $5, $6)
                ''', guild_id, row['user_id'], end, row['channel_id'], reward, interval)
                await _credit(conn, guild_id, row['user_id'], reward)
                await queue_reward_log(conn, guild_id, f"points:{start.isoformat()}:{interval}",
                                       row['user_id'], 'points', reward, None, row['channel_id'], now)
            await conn.execute('''
                UPDATE chat_activity_minutes SET rewarded = TRUE
                WHERE guild_id = $1 AND minute >= $2 AND minute < $3
            ''', guild_id, start.replace(second=0, microsecond=0), end)
        if len(pending) > 500:
            cursor = as_datetime(pending[499]['window_start']) + duration * 2
        elif now >= next_reward:
            cursor = anchor + duration * (int((now - anchor) // duration) + 1)
        else:
            cursor = next_reward
        await conn.execute('UPDATE chat_point_settings SET next_reward_at = $2 WHERE guild_id = $1', guild_id, cursor)
        # 하루 전체 채팅 수가 가장 많은 한 명. 동률이면 먼저 활동한 유저가 받습니다.
        winners = await conn.fetch('''
            SELECT DISTINCT ON (d.activity_day) d.* FROM chat_activity_daily d
            WHERE d.guild_id = $1 AND d.activity_day < $2
            AND NOT EXISTS (SELECT 1 FROM chat_random_boxes b
                WHERE b.guild_id = d.guild_id AND b.activity_day = d.activity_day)
            ORDER BY d.activity_day, d.message_count DESC, d.first_message_at, d.user_id
        ''', guild_id, closed_before) if include_daily else []
        for row in winners:
            inserted = await conn.fetchval('''
                INSERT INTO chat_random_boxes (guild_id, activity_day, user_id, channel_id)
                VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING RETURNING user_id
            ''', guild_id, row['activity_day'], row['user_id'], row['channel_id'])
            if inserted is not None:
                await queue_reward_log(conn, guild_id, f"box:{row['activity_day']}", row['user_id'], 'box',
                                       None, row['activity_day'], row['channel_id'], now)
        await conn.execute('DELETE FROM chat_activity_messages WHERE guild_id = $1 AND sent_at < $2',
                           guild_id, min(now - dt.timedelta(days=3), cursor - duration))
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
        await queue_reward_log(conn, guild_id, f"box_open:{box['activity_day']}", user_id, 'box_open',
                               points, box['activity_day'], box['channel_id'], now)
        return points, await get_balance(conn, guild_id, user_id)


def box_button(guild_id):
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="랜덤박스 열기", emoji="🎁", style=discord.ButtonStyle.success,
                                   custom_id=BOX_PREFIX + str(guild_id)))
    return view


def as_datetime(value):
    return value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))


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
    options = {"ephemeral": True}
    if boxes:
        options["view"] = box_button(guild_id)
    await interaction.followup.send("\n".join(lines), **options)


class ChatPointsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._daily_checked = {}
        self._next_log_check = dt.datetime.min.replace(tzinfo=UTC)

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

    @tasks.loop(seconds=1)
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
                guild_id = setting['guild_id']
                closed_day = (now - dt.timedelta(minutes=2)).astimezone(KST).date()
                daily_due = self._daily_checked.get(guild_id) != closed_day
                reward_due = as_datetime(setting['next_reward_at']) <= now
                if reward_due or daily_due:
                    async with self.bot.db_pool.acquire() as conn:
                        await settle_activity(conn, guild_id, now, include_daily=daily_due)
                    self._daily_checked[guild_id] = closed_day
                    await self.notify_rewards(guild_id)
                if now >= self._next_log_check:
                    await self.send_reward_logs(guild_id, now)
            except Exception:
                LOG.exception("채팅 포인트 정산 실패: guild=%s", setting['guild_id'])
        if now >= self._next_log_check:
            self._next_log_check = now + dt.timedelta(seconds=5)

    @check_activity.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    async def notify_rewards(self, guild_id):
        # 보상은 먼저 적립됩니다. DM 실패는 잔액이나 박스 지급을 취소하지 않습니다.
        async with self.bot.db_pool.acquire() as conn:
            rewards = await conn.fetch('''
                UPDATE chat_point_rewards SET notified = TRUE WHERE (guild_id, user_id, minute, reward_interval_minutes) IN (
                    SELECT guild_id, user_id, minute, reward_interval_minutes FROM chat_point_rewards
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
                                f"채널: <#{row['channel_id']}> · 지급 시간: {timestamp:%m/%d %H:%M}")
                        await user.send(text, allowed_mentions=discord.AllowedMentions.none())
                    else:
                        text = (f"🎁 {row['activity_day']} 최다 채팅 활동자로 선정되어 **랜덤박스 1개**가 지급되었습니다!\n"
                                "아래 버튼을 눌러 열면 **100P~300P**를 획득합니다.")
                        await user.send(text, view=box_button(guild_id), allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    LOG.info("채팅 보상 DM 전송 실패: guild=%s user=%s (/포인트조회패널 이용)", guild_id, row['user_id'])

    async def send_reward_logs(self, guild_id, now):
        async with self.bot.db_pool.acquire() as conn:
            channel_id = await conn.fetchval('SELECT channel_id FROM chat_reward_log_settings WHERE guild_id = $1', guild_id)
            if channel_id is None:
                return
            events = await conn.fetch('''
                UPDATE chat_reward_log_events SET lease_until = $2 + INTERVAL '2 minutes'
                WHERE (guild_id, event_key) IN (
                    SELECT guild_id, event_key FROM chat_reward_log_events
                    WHERE guild_id = $1 AND sent_at IS NULL AND next_attempt <= $2
                        AND (lease_until IS NULL OR lease_until <= $2)
                    ORDER BY occurred_at, event_key LIMIT 50 FOR UPDATE SKIP LOCKED
                ) RETURNING *
            ''', guild_id, now)
        for event in events:
            try:
                await self.bot.http.request(discord.http.Route('POST', f'/channels/{channel_id}/messages'),
                                            json=reward_log_payload(event))
            except discord.HTTPException:
                LOG.warning('지급로그 전송 실패: guild=%s channel=%s', guild_id, channel_id)
                async with self.bot.db_pool.acquire() as conn:
                    await conn.execute('''
                        UPDATE chat_reward_log_events SET lease_until = NULL, next_attempt = $3
                        WHERE guild_id = $1 AND event_key = $2
                    ''', guild_id, event['event_key'], now + dt.timedelta(seconds=30))
            else:
                async with self.bot.db_pool.acquire() as conn:
                    await conn.execute('''
                        UPDATE chat_reward_log_events SET sent_at = $3, lease_until = NULL
                        WHERE guild_id = $1 AND event_key = $2
                    ''', guild_id, event['event_key'], now)


def reward_log_payload(event):
    user = f"<@{event['user_id']}>"
    if event['kind'] == 'box':
        text = (f"## 🎁 일일 랜덤박스 지급\n\n{user}님께 **랜덤박스 1개**를 지급했습니다.\n"
                f"{event['activity_day']} 하루 최다 채팅 유저로 선정되었습니다.")
    elif event['kind'] == 'box_open':
        text = f"## 🎁 랜덤박스 개봉 포인트 지급\n\n{user}님이 랜덤박스를 열어 **{event['points']:,}P**를 획득했습니다."
    else:
        text = f"## 🎉 채팅 이벤트 포인트 지급\n\n{user}님께 추첨으로 **{event['points']:,}P**를 지급했습니다."
    timestamp = as_datetime(event['occurred_at']).astimezone(KST)
    text += f"\n\n> 지급 시각 : {timestamp:%Y-%m-%d %H:%M:%S}\n이벤트 채널 : <#{event['source_channel_id']}>"
    return {'flags': 1 << 15, 'allowed_mentions': {'parse': []}, 'components': [
        {'type': 17, 'accent_color': 0x32CD32, 'components': [{'type': 10, 'content': text}]}]}


async def configure_chat_points(interaction, channel, enabled, interval_minutes=1):
    started_at = getattr(interaction, 'created_at', dt.datetime.now(UTC))
    pool = interaction.client.db_pool
    if pool is None:
        await interaction.response.send_message("데이터베이스가 연결되지 않았습니다.", ephemeral=True)
        return
    channel = channel or interaction.channel
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != interaction.guild.id:
        await interaction.response.send_message("이 서버의 텍스트 채널을 선택해 주세요.", ephemeral=True)
        return
    permissions = channel.permissions_for(interaction.guild.me)
    if enabled and not permissions.view_channel:
        await interaction.response.send_message("봇이 해당 채널을 볼 수 있도록 권한을 설정해 주세요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    async with pool.acquire() as conn:
        await configure_channel(conn, interaction.guild.id, channel.id, enabled, started_at, interval_minutes)
    if enabled:
        text = (f"✅ {channel.mention}에서 채팅 포인트를 지급합니다.\n"
                f"{interval_minutes}분마다 해당 주기 동안 채팅한 유저 중 랜덤 1명에게 10P~50P 지급.\n"
                "한국 시간 자정에 전날 최다 채팅 활동자 1명에게 랜덤박스 지급.\n"
                "보상 알림은 당첨된 유저의 DM으로 전송됩니다.")
    else:
        text = "✅ 채팅 포인트 적립을 중지했습니다. 이미 기록된 활동과 보상은 정산합니다."
    await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def configure_reward_log(interaction, channel):
    if interaction.guild is None or channel.guild.id != interaction.guild.id:
        await interaction.response.send_message('이 서버의 텍스트 채널을 선택해 주세요.', ephemeral=True)
        return
    if interaction.client.db_pool is None:
        await interaction.response.send_message('데이터베이스가 연결되지 않았습니다.', ephemeral=True)
        return
    permissions = channel.permissions_for(interaction.guild.me)
    if not (permissions.view_channel and permissions.send_messages):
        await interaction.response.send_message('봇이 해당 채널을 보고 메시지를 보낼 수 있도록 권한을 설정해 주세요.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    async with interaction.client.db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO chat_reward_log_settings (guild_id, channel_id) VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET channel_id = EXCLUDED.channel_id
        ''', interaction.guild.id, channel.id)
    await interaction.followup.send(f'✅ 채팅 이벤트 지급로그 채널을 {channel.mention}(으)로 설정했습니다.\n포인트 지급·일일 랜덤박스 지급·박스 개봉 포인트를 기록합니다.',
                                    ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
