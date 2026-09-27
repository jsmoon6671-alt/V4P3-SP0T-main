"""봇 자체 가입 지원서 패널과 관리진 알림."""

import datetime as dt
import json
import logging
import secrets

import discord


LOG = logging.getLogger(__name__)
KST = dt.timezone(dt.timedelta(hours=9))
PANEL_PREFIX = "join_application:"
DECISION_PREFIX = "join_application_decision:"


async def initialize_join_application_schema(conn):
    async with conn.transaction():
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS join_application_settings (
                guild_id BIGINT PRIMARY KEY,
                alert_channel_id BIGINT,
                staff_role_id BIGINT
            );
            CREATE TABLE IF NOT EXISTS join_application_panels (
                token TEXT PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                questions JSONB NOT NULL,
                created_by BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS join_application_submissions (
                guild_id BIGINT NOT NULL,
                interaction_id BIGINT NOT NULL,
                panel_token TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                answers JSONB NOT NULL,
                submitted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'PENDING',
                reviewer_id BIGINT,
                reviewed_at TIMESTAMPTZ,
                notification_channel_id BIGINT,
                notification_message_id BIGINT,
                PRIMARY KEY (guild_id, interaction_id)
            );
            ALTER TABLE join_application_submissions ADD COLUMN IF NOT EXISTS status
                TEXT NOT NULL DEFAULT 'PENDING';
            ALTER TABLE join_application_submissions ADD COLUMN IF NOT EXISTS reviewer_id BIGINT;
            ALTER TABLE join_application_submissions ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ;
            ALTER TABLE join_application_submissions ADD COLUMN IF NOT EXISTS notification_channel_id BIGINT;
            ALTER TABLE join_application_submissions ADD COLUMN IF NOT EXISTS notification_message_id BIGINT;
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'join_application_status_check'
                      AND conrelid = 'join_application_submissions'::regclass) THEN
                    ALTER TABLE join_application_submissions
                        ADD CONSTRAINT join_application_status_check
                        CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED'));
                END IF;
            END $$;
        ''')


async def set_alert_channel(interaction, channel):
    if interaction.guild is None or channel.guild.id != interaction.guild.id:
        await interaction.response.send_message("❌ 이 서버의 텍스트 채널을 선택해 주세요.", ephemeral=True)
        return
    if interaction.client.db_pool is None:
        await interaction.response.send_message("❌ 데이터베이스가 연결되지 않았습니다.", ephemeral=True)
        return
    permissions = channel.permissions_for(interaction.guild.me)
    if not (permissions.view_channel and permissions.send_messages):
        await interaction.response.send_message("❌ 봇이 해당 채널을 보고 메시지를 보낼 수 있도록 권한을 설정해 주세요.", ephemeral=True)
        return
    async with interaction.client.db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO join_application_settings (guild_id, alert_channel_id) VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET alert_channel_id = EXCLUDED.alert_channel_id
        ''', interaction.guild.id, channel.id)
    await interaction.response.send_message(
        f"✅ 가입 지원서 알림 채널을 {channel.mention}(으)로 설정했습니다.", ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def set_staff_role(interaction, role):
    if interaction.guild is None or role.guild.id != interaction.guild.id or role.is_default():
        await interaction.response.send_message("❌ 이 서버의 관리진 역할을 선택해 주세요.", ephemeral=True)
        return
    if not role.mentionable and not interaction.guild.me.guild_permissions.mention_everyone:
        await interaction.response.send_message(
            "❌ 해당 역할을 멘션 가능으로 설정하거나 봇에 `@everyone, @here 및 모든 역할 멘션` 권한을 부여해 주세요.",
            ephemeral=True,
        )
        return
    if interaction.client.db_pool is None:
        await interaction.response.send_message("❌ 데이터베이스가 연결되지 않았습니다.", ephemeral=True)
        return
    async with interaction.client.db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO join_application_settings (guild_id, staff_role_id) VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET staff_role_id = EXCLUDED.staff_role_id
        ''', interaction.guild.id, role.id)
    await interaction.response.send_message(
        f"✅ 가입 지원 알림에서 멘션할 관리진 역할을 {role.mention}(으)로 설정했습니다.", ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


def normalize_questions(*questions):
    result = []
    for question in questions:
        if question is None:
            continue
        question = " ".join(str(question).split()).strip()
        if not 1 <= len(question) <= 45:
            raise ValueError("각 질문은 1~45자로 입력해 주세요.")
        result.append(question)
    if not result:
        raise ValueError("질문을 한 개 이상 입력해 주세요.")
    if len(set(result)) != len(result):
        raise ValueError("같은 질문을 중복해서 입력할 수 없습니다.")
    return result


def panel_payload(question_count):
    content = (
        "## 📝 가입 지원서\n\n"
        "아래 **지원서 작성** 버튼을 눌러 가입 지원서를 작성해 주세요.\n"
        f"질문은 총 **{question_count}개**이며, 제출한 내용은 관리진에게 전달됩니다."
    )
    return {
        "flags": 1 << 15,
        "allowed_mentions": {"parse": []},
        "components": [{"type": 17, "accent_color": 0x32CD32, "components": [
            {"type": 10, "content": content},
        ]}],
    }


async def create_application_panel(interaction, questions):
    if interaction.client.db_pool is None:
        await interaction.response.send_message("❌ 데이터베이스가 연결되지 않았습니다.", ephemeral=True)
        return
    async with interaction.client.db_pool.acquire() as conn:
        settings = await conn.fetchrow(
            'SELECT * FROM join_application_settings WHERE guild_id = $1', interaction.guild.id,
        )
        if not settings or not settings['alert_channel_id'] or not settings['staff_role_id']:
            await interaction.response.send_message(
                "❌ 먼저 `/가입지원알림`으로 채널을, `/관리진`으로 역할을 설정해 주세요.", ephemeral=True,
            )
            return
        token = secrets.token_urlsafe(12)
        await conn.execute('''
            INSERT INTO join_application_panels (token, guild_id, questions, created_by)
            VALUES ($1, $2, $3::jsonb, $4)
        ''', token, interaction.guild.id, json.dumps(questions, ensure_ascii=False), interaction.user.id)
    payload = panel_payload(len(questions))
    payload['components'][0]['components'].append({"type": 1, "components": [{
        "type": 2, "style": 1, "label": "지원서 작성", "emoji": {"name": "📝"},
        "custom_id": PANEL_PREFIX + token,
    }]})
    try:
        await interaction.client.http.request(
            discord.http.Route("POST", f"/interactions/{interaction.id}/{interaction.token}/callback"),
            json={"type": 4, "data": payload},
        )
    except Exception:
        async with interaction.client.db_pool.acquire() as conn:
            await conn.execute('DELETE FROM join_application_panels WHERE token = $1', token)
        raise


def safe_codeblock(value):
    # 답변이 코드블록을 탈출하거나 역할을 멘션하지 못하게 합니다.
    value = discord.utils.escape_mentions(str(value).strip()).replace("```", "`\u200b``")
    return value or "(답변 없음)"


def decode_json(value):
    return json.loads(value) if isinstance(value, str) else list(value)


def as_datetime(value):
    return value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def application_payload(role_id, user_id, questions, answers, submitted_at, guild_id, submission_id,
                        status='PENDING', reviewer_id=None, reviewed_at=None):
    lines = [f"<@&{role_id}>", "## 📝 새로운 가입 지원서", "", f"지원자 : <@{user_id}> (`{user_id}`)"]
    for question, answer in zip(questions, answers):
        safe_question = discord.utils.escape_markdown(discord.utils.escape_mentions(question))
        lines.extend(["", f"**{safe_question}**", f"```\n{safe_codeblock(answer)}\n```"])
    lines.extend(["", f"> 제출 시각 : {as_datetime(submitted_at).astimezone(KST):%Y-%m-%d %H:%M:%S}"])
    if status != 'PENDING':
        label = "✅ 승인" if status == 'APPROVED' else "❌ 거절"
        lines.extend(["", f"**처리 결과 : {label}**", f"처리 관리진 : <@{reviewer_id}>",
                      f"> 처리 시각 : {as_datetime(reviewed_at).astimezone(KST):%Y-%m-%d %H:%M:%S}"])
    components = [{"type": 10, "content": "\n".join(lines)}]
    if status == 'PENDING':
        components.append({"type": 1, "components": [
            {"type": 2, "style": 3, "label": "승인", "emoji": {"name": "✅"},
             "custom_id": f"{DECISION_PREFIX}approve:{guild_id}:{submission_id}"},
            {"type": 2, "style": 4, "label": "거절", "emoji": {"name": "❌"},
             "custom_id": f"{DECISION_PREFIX}reject:{guild_id}:{submission_id}"},
        ]})
    return {
        "flags": 1 << 15,
        "allowed_mentions": ({"parse": [], "roles": [str(role_id)]}
                             if status == 'PENDING' else {"parse": []}),
        "components": [{"type": 17, "accent_color": 0x32CD32, "components": components}],
    }


class JoinApplicationModal(discord.ui.Modal):
    def __init__(self, bot, token, guild_id, questions):
        super().__init__(title="가입 지원서 작성", timeout=900)
        self.bot, self.token, self.guild_id = bot, token, guild_id
        self.questions = questions
        for index, question in enumerate(questions):
            self.add_item(discord.ui.TextInput(
                label=question, custom_id=f"answer_{index}", style=discord.TextStyle.paragraph,
                min_length=1, max_length=600, required=True,
            ))

    async def on_submit(self, interaction):
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await interaction.response.send_message("❌ 이 지원서가 생성된 서버에서 제출해 주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        answers = [item.value for item in self.children]
        submitted_at = dt.datetime.now(dt.timezone.utc)
        try:
            async with self.bot.db_pool.acquire() as conn:
                async with conn.transaction():
                    panel = await conn.fetchrow(
                        'SELECT questions FROM join_application_panels WHERE token = $1 AND guild_id = $2',
                        self.token, self.guild_id,
                    )
                    settings = await conn.fetchrow(
                        'SELECT * FROM join_application_settings WHERE guild_id = $1', self.guild_id,
                    )
                    if not panel or not settings or not settings['alert_channel_id'] or not settings['staff_role_id']:
                        await interaction.followup.send("❌ 지원서 설정을 찾을 수 없습니다. 관리자에게 문의해 주세요.", ephemeral=True)
                        return
                    saved_questions = decode_json(panel['questions'])
                    if saved_questions != self.questions:
                        await interaction.followup.send("❌ 지원서 질문이 변경되었습니다. 패널에서 다시 작성해 주세요.", ephemeral=True)
                        return
                    inserted = await conn.fetchval('''
                        INSERT INTO join_application_submissions
                            (guild_id, interaction_id, panel_token, user_id, answers, submitted_at)
                        VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                        ON CONFLICT DO NOTHING RETURNING interaction_id
                    ''', self.guild_id, interaction.id, self.token, interaction.user.id,
                         json.dumps(answers, ensure_ascii=False), submitted_at)
                    if inserted is None:
                        await interaction.followup.send("이미 제출 처리된 지원서입니다.", ephemeral=True)
                        return
                    payload = application_payload(
                        settings['staff_role_id'], interaction.user.id, self.questions, answers, submitted_at,
                        self.guild_id, interaction.id,
                    )
                    try:
                        sent = await self.bot.http.request(
                            discord.http.Route("POST", f"/channels/{settings['alert_channel_id']}/messages"),
                            json=payload,
                        )
                        message_id = int(sent['id']) if isinstance(sent, dict) and sent.get('id') else None
                        await conn.execute('''
                            UPDATE join_application_submissions
                            SET notification_channel_id = $3, notification_message_id = $4
                            WHERE guild_id = $1 AND interaction_id = $2
                        ''', self.guild_id, interaction.id, settings['alert_channel_id'], message_id)
                    except Exception:
                        await conn.execute(
                            'DELETE FROM join_application_submissions WHERE guild_id = $1 AND interaction_id = $2',
                            self.guild_id, interaction.id,
                        )
                        raise
        except Exception:
            LOG.exception("가입 지원서 알림 전송 실패: guild=%s user=%s", self.guild_id, interaction.user.id)
            await interaction.followup.send("❌ 지원서 전송에 실패했습니다. 잠시 후 다시 제출해 주세요.", ephemeral=True)
            return
        await interaction.followup.send("✅ 가입 지원서가 관리진에게 전달되었습니다.", ephemeral=True)


async def handle_application_button(interaction, pool):
    if pool is None or interaction.guild is None:
        await interaction.response.send_message("❌ 서버와 데이터베이스 연결을 확인해 주세요.", ephemeral=True)
        return
    custom_id = (interaction.data or {}).get('custom_id', '')
    token = custom_id[len(PANEL_PREFIX):]
    if not token or len(token) > 32:
        await interaction.response.send_message("❌ 유효하지 않은 지원서 패널입니다.", ephemeral=True)
        return
    async with pool.acquire() as conn:
        panel = await conn.fetchrow(
            'SELECT guild_id, questions FROM join_application_panels WHERE token = $1', token,
        )
    if not panel or panel['guild_id'] != interaction.guild.id:
        await interaction.response.send_message("❌ 만료되었거나 다른 서버의 지원서 패널입니다.", ephemeral=True)
        return
    questions = decode_json(panel['questions'])
    if not 1 <= len(questions) <= 5:
        await interaction.response.send_message("❌ 지원서 질문 설정이 올바르지 않습니다.", ephemeral=True)
        return
    await interaction.response.send_modal(
        JoinApplicationModal(interaction.client, token, interaction.guild.id, questions),
    )


async def handle_application_decision(interaction, pool):
    if pool is None or interaction.guild is None:
        await interaction.response.send_message("❌ 서버와 데이터베이스 연결을 확인해 주세요.", ephemeral=True)
        return
    parts = (interaction.data or {}).get('custom_id', '').split(':')
    if (len(parts) != 4 or parts[0] != DECISION_PREFIX.rstrip(':')
            or parts[1] not in ('approve', 'reject')
            or not parts[2].isdigit() or not parts[3].isdigit()):
        await interaction.response.send_message("❌ 유효하지 않은 지원서 처리 버튼입니다.", ephemeral=True)
        return
    action, guild_id, submission_id = parts[1], int(parts[2]), int(parts[3])
    if guild_id != interaction.guild.id or not (0 < submission_id <= 9_223_372_036_854_775_807):
        await interaction.response.send_message("❌ 다른 서버이거나 유효하지 않은 지원서입니다.", ephemeral=True)
        return
    async with pool.acquire() as conn:
        settings = await conn.fetchrow(
            'SELECT * FROM join_application_settings WHERE guild_id = $1', guild_id,
        )
    staff_role_id = settings['staff_role_id'] if settings else None
    is_admin = getattr(getattr(interaction.user, 'guild_permissions', None), 'administrator', False)
    has_staff_role = staff_role_id is not None and any(
        role.id == staff_role_id for role in getattr(interaction.user, 'roles', ())
    )
    if not (is_admin or has_staff_role):
        await interaction.response.send_message("❌ 설정된 관리진 역할 또는 관리자만 처리할 수 있습니다.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    status = 'APPROVED' if action == 'approve' else 'REJECTED'
    reviewed_at = dt.datetime.now(dt.timezone.utc)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                submission = await conn.fetchrow('''
                    SELECT * FROM join_application_submissions
                    WHERE guild_id = $1 AND interaction_id = $2 FOR UPDATE
                ''', guild_id, submission_id)
                if submission is None:
                    await interaction.followup.send("❌ 지원서 제출 기록을 찾을 수 없습니다.", ephemeral=True)
                    return
                if submission['status'] != 'PENDING':
                    result = "승인" if submission['status'] == 'APPROVED' else "거절"
                    await interaction.followup.send(f"이미 **{result}** 처리된 지원서입니다.", ephemeral=True)
                    return
                panel = await conn.fetchrow(
                    'SELECT questions FROM join_application_panels WHERE token = $1', submission['panel_token'],
                )
                if panel is None or settings is None:
                    await interaction.followup.send("❌ 지원서 패널 또는 설정을 찾을 수 없습니다.", ephemeral=True)
                    return
                message_id = submission['notification_message_id'] or interaction.message.id
                channel_id = submission['notification_channel_id'] or interaction.channel_id
                if submission['notification_message_id'] and interaction.message.id != submission['notification_message_id']:
                    await interaction.followup.send("❌ 원본 지원서 알림에서 처리해 주세요.", ephemeral=True)
                    return
                payload = application_payload(
                    settings['staff_role_id'], submission['user_id'], decode_json(panel['questions']),
                    decode_json(submission['answers']), submission['submitted_at'], guild_id, submission_id,
                    status=status, reviewer_id=interaction.user.id, reviewed_at=reviewed_at,
                )
                await interaction.client.http.request(
                    discord.http.Route("PATCH", f"/channels/{channel_id}/messages/{message_id}"), json=payload,
                )
                await conn.execute('''
                    UPDATE join_application_submissions
                    SET status = $3, reviewer_id = $4, reviewed_at = $5,
                        notification_channel_id = $6, notification_message_id = $7
                    WHERE guild_id = $1 AND interaction_id = $2
                ''', guild_id, submission_id, status, interaction.user.id, reviewed_at, channel_id, message_id)
    except Exception:
        LOG.exception("가입 지원서 승인·거절 처리 실패: guild=%s submission=%s", guild_id, submission_id)
        await interaction.followup.send("❌ 지원서 처리에 실패했습니다. 잠시 후 다시 눌러 주세요.", ephemeral=True)
        return
    dm_sent = True
    try:
        user = interaction.client.get_user(submission['user_id']) or await interaction.client.fetch_user(submission['user_id'])
        result = "승인" if status == 'APPROVED' else "거절"
        icon = "✅" if status == 'APPROVED' else "❌"
        await user.send(f"{icon} V4P3 SP0T 가입 지원서가 **{result}**되었습니다.",
                        allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        LOG.info("가입 지원서 결과 DM 전송 실패: guild=%s user=%s", guild_id, submission['user_id'])
        dm_sent = False
    result = "승인" if status == 'APPROVED' else "거절"
    message = f"✅ 지원서를 **{result}** 처리했습니다."
    if not dm_sent:
        message += " 지원자의 DM이 닫혀 있어 결과 알림은 전송하지 못했습니다."
    await interaction.followup.send(message, ephemeral=True)
