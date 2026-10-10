import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import random
import string
import asyncpg
import os
import time
import aiohttp
import chat_exporter
import io
import asyncio
import logging
import json
from bs4 import BeautifulSoup
from event_broadcast import register_event_command
from store_lookup import brand_selector, lookup_panel, handle_brand_selection
from chat_points import (
    ChatPointsCog,
    configure_chat_points, handle_reward_interaction, configure_reward_log,
)
from join_applications import (
    set_alert_channel, set_staff_role,
    normalize_questions, create_application_panel, handle_application_button,
    handle_application_decision,
)
from loyalty_points import (
    REVIEW_GUIDE, PointsError, get_balance,
    award_review_points, parse_amount, parse_points, request_payment,
    reset_failed_payment, resolve_payment, cancel_order_record, payment_summary, maximum_points, adjust_points,
)
from delivery_tracking import (
    DeliveryTrackingError, format_tracking_result,
    normalize_waybill, safe_text, track_shipment,
)
from review_watermark import ReviewImageError, watermark_review_image
from admin_roles import register_admin_role_command
from chat_ranking import (
    ChatRankingCog,
    handle_rank_page,
    register_chat_ranking_commands,
)
from database import initialize_database
from order_editing import build_purchase_log_payload, register_order_edit_command
from web_store import start_web_store

# 한국 표준시(KST) 설정
KST = datetime.timezone(datetime.timedelta(hours=9))

# 1. 봇 권한(Intents) 설정
intents = discord.Intents.default()
intents.message_content = True
intents.members = True # 멤버 관리를 위해 (역할 지급 용도)

# ==========================================
# [핵심 함수] V2 컴포넌트 자동 생성기
# ==========================================
def create_v2_payload(content: str, color: int = 0x32CD32, extra_components: list = None, ephemeral: bool = False):
    base_components = [{"type": 10, "content": content}]
    
    if extra_components:
        base_components.extend(extra_components)
        
    flags = (1 << 15) | (1 << 6) if ephemeral else (1 << 15)
    
    return {
        "flags": flags,
        "components": [
            {
                "type": 17,
                "accent_color": color,
                "components": base_components
            }
        ]
    }


def create_tier_upgrade_payload(buyer_id: int, role_mention: str, total_spent: int):
    customer_mention = f"<@{buyer_id}>"
    content = (
        "## 🎉 VIP 등급 업그레이드!\n\n"
        "`👤` **고객**\n"
        f"{customer_mention}\n\n"
        "`✨` **새로운 등급**\n"
        f"{role_mention}\n\n"
        "`💰` **누적 구매액**\n"
        f"`{total_spent:,}원`\n\n"
        "**```앞으로도 많은 이용 부탁드립니다!```**"
    )
    payload = create_v2_payload(content)
    payload["allowed_mentions"] = {"parse": [], "users": [str(buyer_id)]}
    return payload


async def replace_review_guide_message(conn, http, guild_id: int, channel_id: int, content: str):
    """기존 후기 안내를 지우고 새 안내를 맨 아래에 전송합니다."""
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT (guild_id) DO NOTHING",
            guild_id,
        )
        settings = await conn.fetchrow(
            """
            SELECT review_channel_id, review_auto_message_id, review_auto_message_channel_id
            FROM guild_settings
            WHERE guild_id = $1
            FOR UPDATE
            """,
            guild_id,
        )

        old_message_id = settings.get('review_auto_message_id') if settings else None
        old_channel_id = settings.get('review_auto_message_channel_id') if settings else None
        if old_message_id:
            delete_channel_id = old_channel_id or settings.get('review_channel_id') or channel_id
            try:
                await http.request(
                    discord.http.Route(
                        "DELETE",
                        f"/channels/{delete_channel_id}/messages/{old_message_id}",
                    )
                )
            except discord.NotFound:
                pass

        response = await http.request(
            discord.http.Route("POST", f"/channels/{channel_id}/messages"),
            json=create_v2_payload(content),
        )
        if not response or not response.get('id'):
            raise RuntimeError("후기 안내 메시지 ID를 확인할 수 없습니다.")

        message_id = int(response['id'])
        await conn.execute(
            """
            UPDATE guild_settings
            SET review_auto_message_id = $2,
                review_auto_message_channel_id = $3
            WHERE guild_id = $1
            """,
            guild_id,
            message_id,
            channel_id,
        )
        return message_id

class AdminCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction):
        data = interaction.data or {}
        is_review = data.get("type", 1) == 1 and data.get("name") == "후기작성"
        permissions = getattr(interaction.user, "guild_permissions", None)
        if interaction.guild is not None and (is_review or (permissions and permissions.administrator)):
            return True
        if interaction.type == discord.InteractionType.autocomplete:
            await interaction.response.autocomplete([])
        else:
            message = "❌ 서버에서만 사용할 수 있습니다." if interaction.guild is None else "❌ 관리자만 사용할 수 있습니다."
            await interaction.response.send_message(message, ephemeral=True)
        return False

    async def sync(self, *, guild=None):
        for command in self.get_commands(guild=guild):
            is_review = isinstance(command, app_commands.Command) and command.name == "후기작성"
            command.default_permissions = None if is_review else discord.Permissions(administrator=True)
            command.guild_only = True
        return await super().sync(guild=guild)


async def initialize_join_logs(conn):
    await conn.execute('ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS join_log_channel_id BIGINT;')


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents, tree_cls=AdminCommandTree)
        self.db_pool = None
        self.web_store = None
        self.refresh_purchase_leaderboard = None

    async def setup_hook(self):
        # Railway의 DATABASE_URL 환경 변수를 가져와서 PostgreSQL 연결
        db_url = os.environ.get("DATABASE_URL")
        if db_url:
            self.db_pool = await asyncpg.create_pool(db_url)
            print("✅ PostgreSQL 데이터베이스 연결에 성공했습니다!")
            
            async with self.db_pool.acquire() as conn:
                await initialize_database(conn)
        else:
            print("⚠️ DATABASE_URL이 설정되지 않아 DB 기능을 사용할 수 없습니다.")

        await self.tree.sync()
        if self.db_pool is not None:
            await self.add_cog(ChatPointsCog(self))
            await self.add_cog(ChatRankingCog(self))
            self.refresh_purchase_leaderboard = update_leaderboard
            if os.getenv("WEB_STORE_ENABLED", "true").lower() not in {"0", "false", "off", "no"}:
                self.web_store = await start_web_store(self)
        leaderboard_updater.start()  # 실시간 랭킹 및 인기 품목 루프 시작
        print('✅ 슬래시 명령어 동기화 및 랭킹/인기 시스템이 시작되었습니다!')

    async def close(self):
        if self.web_store is not None:
            await self.web_store.close()
            self.web_store = None
        if self.db_pool is not None:
            await self.db_pool.close()
            self.db_pool = None
        await super().close()

bot = MyBot()
register_event_command(bot)
register_admin_role_command(bot)
register_chat_ranking_commands(bot)


# ==========================================
# [기능 1] URL.KR 사이트 연동 배송조회 시스템 
# ==========================================
class TrackingModal(discord.ui.Modal):
    def __init__(self, courier_name: str, courier_code: str):
        super().__init__(title=f'{courier_name} 배송 조회'[:45])
        self.courier_name = courier_name
        self.courier_code = courier_code

    waybill = discord.ui.TextInput(
        label='운송장 번호',
        placeholder='운송장 번호를 숫자만 입력해 주세요.',
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            waybill_num = normalize_waybill(self.waybill.value)
            data = await track_shipment(self.courier_code, waybill_num)
            result_text = format_tracking_result(self.courier_name, waybill_num, data)
        except DeliveryTrackingError as exc:
            await interaction.followup.send(
                f"❌ 배송 조회에 실패했습니다: {safe_text(str(exc), limit=600)}",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        result_payload = create_v2_payload(result_text, ephemeral=True)
        result_payload["allowed_mentions"] = {"parse": []}
        await interaction.client.http.request(
            discord.http.Route("POST", f"/webhooks/{interaction.application_id}/{interaction.token}"),
            json=result_payload,
        )


@bot.tree.command(name="배송조회", description="배송 조회 패널을 띄워줍니다.")
async def send_tracking_panel(interaction: discord.Interaction):
    await interaction.response.defer()
    carriers = [
        {"name": "GS반값택배", "id": "kr.cvsnet", "emoji": "🏪"},
        {"name": "CU알뜰택배", "id": "kr.cupost", "emoji": "🏪"},
        {"name": "CJ대한통운", "id": "kr.cjlogistics", "emoji": "🚚"},
        {"name": "우체국택배", "id": "kr.epost", "emoji": "📮"},
    ]

    main_content = (
        "## 📦 배송조회\n\n"
        "- 아래에 버튼을 눌러 배송현황을 실시간으로 조회하실 수 있습니다.\n\n"
        "- 🟢 24시간 실시간 배송조회\n"
        "- 🌟 간편하고 빠른 배송조회\n\n"
        "- `📦` 이용방법\n"
        "```아래에서 배송조회할 택배사를 선택해 주세요.```\n"
        "```택배 운송장 번호를 입력해 주세요.```\n"
        "```배송 현황을 쉽고 빠르게 확인해 보세요.```"
    )

    main_payload = {
        "flags": 1 << 15,
        "components": [
            {
                "type": 17,
                "accent_color": 0x32CD32,
                "components": [
                    {
                        "type": 10, 
                        "content": main_content
                    },
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 3,
                                "custom_id": "select_courier",
                                "placeholder": "📦 배송조회할 택배사를 선택해 주세요",
                                "options": [
                                    {
                                        "label": carrier["name"],
                                        "value": f"{carrier['name']}|{carrier['id']}",
                                        "emoji": {"name": carrier["emoji"]},
                                    }
                                    for carrier in carriers
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
    }
    
    main_payload["allowed_mentions"] = {"parse": []}
    await interaction.client.http.request(
        discord.http.Route("PATCH", f"/webhooks/{interaction.application_id}/{interaction.token}/messages/@original"),
        json=main_payload,
    )


# ==========================================
# [기능 2] 채널 설정 관련 명령어 모음
# ==========================================
async def update_setting(interaction: discord.Interaction, column: str, value: int, success_msg: str):
    if not bot.db_pool: 
        await interaction.response.send_message("❌ DB가 연결되지 않았습니다.", ephemeral=True)
        return
        
    async with bot.db_pool.acquire() as conn:
        await conn.execute(f'''
            INSERT INTO guild_settings (guild_id, {column}) 
            VALUES ($1, $2) 
            ON CONFLICT (guild_id) DO UPDATE SET {column} = $2;
        ''', interaction.guild.id, value)
        
    await interaction.response.send_message(success_msg, ephemeral=True)

@bot.tree.command(name="계좌정보변경", description="입금받을 계좌 정보를 설정하거나 변경합니다. (관리자 전용)")
@app_commands.describe(
    은행명="입금받을 은행 이름을 입력해 주세요. (예: 토스뱅크)",
    계좌번호="입금받을 계좌번호를 입력해 주세요. (예: 1002-5778-3501)",
    예금주="예금주 성함을 입력해 주세요. (예: 문*서)"
)
async def set_account_info(interaction: discord.Interaction, 은행명: str, 계좌번호: str, 예금주: str):
    if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ 관리자만 설정할 수 있습니다.", ephemeral=True)
        return
        
    if not bot.db_pool: 
        await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
        return
        
    async with bot.db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO guild_settings (guild_id, bank_name, account_number, account_holder) 
            VALUES ($1, $2, $3, $4) 
            ON CONFLICT (guild_id) DO UPDATE 
            SET bank_name = $2, account_number = $3, account_holder = $4;
        ''', interaction.guild.id, 은행명, 계좌번호, 예금주)
        
    await interaction.response.send_message(f"✅ 계좌 정보가 성공적으로 변경되었습니다!\n- **은행:** `{은행명}`\n- **계좌번호:** `{계좌번호}`\n- **예금주:** `{예금주}`", ephemeral=True)

@bot.tree.command(name="결제승인채널", description="결제 승인 패널이 올라올 채널을 설정합니다.")
async def set_approval_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    await update_setting(interaction, "approval_channel_id", 채널.id, f"✅ 결제 승인 채널이 {채널.mention}(으)로 설정되었습니다.")


@bot.tree.command(name="입장로그", description="새 멤버의 환영 컨테이너를 보낼 채널을 설정합니다. (관리자 전용)")
@app_commands.describe(채널="입장 환영 메시지를 보낼 텍스트 채널")
async def set_join_log_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    if interaction.guild is None or 채널.guild.id != interaction.guild.id:
        await interaction.response.send_message("❌ 이 서버의 텍스트 채널을 선택해 주세요.", ephemeral=True)
        return
    permissions = 채널.permissions_for(interaction.guild.me)
    if not (permissions.view_channel and permissions.send_messages):
        await interaction.response.send_message("❌ 봇이 해당 채널을 보고 메시지를 보낼 수 있도록 권한을 설정해 주세요.", ephemeral=True)
        return
    await update_setting(interaction, "join_log_channel_id", 채널.id,
                         f"✅ 입장로그 채널이 {채널.mention}(으)로 설정되었습니다. 새 멤버가 입장하면 환영 메시지와 서버 이미지를 보냅니다.")


@bot.tree.command(name="가입지원알림", description="가입 지원서와 관리진 멘션을 보낼 채널을 설정합니다. (관리자 전용)")
@app_commands.describe(채널="가입 지원서 알림을 받을 텍스트 채널")
async def set_join_application_alert(interaction: discord.Interaction, 채널: discord.TextChannel):
    await set_alert_channel(interaction, 채널)


@bot.tree.command(name="관리진", description="가입 지원서 알림에서 멘션할 관리진 역할을 설정합니다. (관리자 전용)")
@app_commands.describe(역할="지원서 접수 시 멘션할 관리진 역할")
async def set_join_application_staff(interaction: discord.Interaction, 역할: discord.Role):
    await set_staff_role(interaction, 역할)


@bot.tree.command(name="가입지원패널", description="유저가 작성할 가입 지원서 패널을 생성합니다. (관리자 전용)")
@app_commands.describe(
    질문1="지원서 첫 번째 질문", 질문2="두 번째 질문 (선택)", 질문3="세 번째 질문 (선택)",
    질문4="네 번째 질문 (선택)", 질문5="다섯 번째 질문 (선택)",
)
async def send_join_application_panel(interaction: discord.Interaction, 질문1: str, 질문2: str = None,
                                      질문3: str = None, 질문4: str = None, 질문5: str = None):
    try:
        questions = normalize_questions(질문1, 질문2, 질문3, 질문4, 질문5)
    except ValueError as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        return
    await create_application_panel(interaction, questions)


@bot.event
async def on_member_join(member: discord.Member):
    if bot.db_pool is None:
        return
    try:
        async with bot.db_pool.acquire() as conn:
            channel_id = await conn.fetchval('SELECT join_log_channel_id FROM guild_settings WHERE guild_id = $1', member.guild.id)
        if channel_id is None:
            return
        count = member.guild.member_count
        if count is None:
            count = len(member.guild.members)
        content = (
            f"### **{member.mention}님, 환영합니다!**\n\n"
            "- V4P3 SP0T에 오신것을 환영 합니다!\n\n"
            f"> 현재 서버 총인원 : {count:,}명\n\n"
            "<#1553617924487651348> 채널에서 인증을 하시면 서버 이용이 가능 합니다!\n\n"
            "<#1553642147490697306> 채널을 꼭 확인해 주세요!"
        )
        media = []
        if member.guild.icon:
            media.append({"type": 12, "items": [{"media": {"url": str(member.guild.icon.with_size(512).url)},
                                                "description": "서버 이미지"}]})
        payload = create_v2_payload(content, extra_components=media)
        payload["allowed_mentions"] = {"parse": [], "users": [str(member.id)]}
        await bot.http.request(discord.http.Route("POST", f"/channels/{channel_id}/messages"), json=payload)
    except Exception:
        logging.exception("입장로그 전송 실패: guild=%s user=%s", member.guild.id, member.id)


@bot.tree.command(name="구매로그", description="구매 승인 시 로그가 올라올 채널을 설정합니다.")
async def set_log_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    await update_setting(interaction, "log_channel_id", 채널.id, f"✅ 구매 로그 채널이 {채널.mention}(으)로 설정되었습니다.")


@bot.tree.command(name="익명역할", description="구매로그에서 구매자 대신 표시할 익명 역할을 설정합니다. (관리자 전용)")
@app_commands.describe(역할="구매로그에 구매자 대신 멘션할 역할을 선택해 주세요.")
async def set_anonymous_role(interaction: discord.Interaction, 역할: discord.Role):
    if 역할.is_default():
        await interaction.response.send_message("❌ 기본 역할은 익명 역할로 설정할 수 없습니다.", ephemeral=True)
        return
    await update_setting(
        interaction,
        "anonymous_role_id",
        역할.id,
        f"✅ 구매로그 익명 역할이 {역할.mention}(으)로 설정되었습니다.",
    )

@bot.tree.command(name="등급업알림", description="유저의 VIP 등급이 승급될 때 알림이 전송될 채널을 설정합니다.")
async def set_tier_log_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    await update_setting(interaction, "tier_log_channel_id", 채널.id, f"✅ 등급 업그레이드 알림 채널이 {채널.mention}(으)로 설정되었습니다.")

@bot.tree.command(name="구매자역할", description="1원 이상 결제 승인 시 자동으로 지급할 역할을 설정합니다.")
async def set_buyer_role(interaction: discord.Interaction, 역할: discord.Role):
    await update_setting(interaction, "buyer_role_id", 역할.id, f"✅ 구매자 역할이 {역할.mention}(으)로 설정되었습니다.")

@bot.tree.command(name="후기채널", description="유저가 작성한 구매 후기가 업로드될 채널을 설정합니다.")
async def set_review_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    await update_setting(interaction, "review_channel_id", 채널.id, f"✅ 후기 채널이 {채널.mention}(으)로 설정되었습니다.")

@bot.tree.command(name="구매자정보", description="구매 승인 시 구매자의 상세 배송 정보가 전송될 채널을 설정합니다. (관리자 전용)")
async def set_buyer_info_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ 관리자만 설정할 수 있습니다.", ephemeral=True)
        return
    await update_setting(interaction, "buyer_info_channel_id", 채널.id, f"✅ 구매자 정보 채널이 {채널.mention}(으)로 설정되었습니다.")

@bot.tree.command(name="후기자동메시지", description="후기가 등록될 때마다 후기 채널에 자동으로 이어서 전송될 메시지를 설정합니다. (관리자 전용)")
@app_commands.describe(메시지="자동으로 전송될 메시지를 입력해 주세요. (미리 복사해 붙여넣거나 \\n 입력 시 줄바꿈 지원)")
async def set_review_auto_message(interaction: discord.Interaction, 메시지: str):
    if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ 관리자만 설정할 수 있습니다.", ephemeral=True)
        return
        
    if not bot.db_pool:
        await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
        return
        
    real_msg = 메시지.replace('\\n', '\n')
    
    async with bot.db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO guild_settings (guild_id, review_auto_message) 
            VALUES ($1, $2) 
            ON CONFLICT (guild_id) DO UPDATE SET review_auto_message = $2;
        ''', interaction.guild.id, real_msg)
        
    await interaction.response.send_message("✅ 후기 자동 안내 메시지가 성공적으로 설정되었습니다!", ephemeral=True)


# ==========================================
# [기능 3] 정보 등록/수정/조회 (USER INFO)
# ==========================================
@bot.tree.command(name="후기안내", description="후기 채널에 포인트 안내를 보내고 후기 등록 때마다 자동 전송하도록 설정합니다.")
@app_commands.describe(채널="안내와 후기가 등록될 채널입니다. 생략하면 기존 후기 채널 또는 현재 채널을 사용합니다.")
async def set_review_guide(interaction: discord.Interaction, 채널: discord.TextChannel = None):
    if not bot.db_pool:
        await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        async with bot.db_pool.acquire() as conn:
            settings = await conn.fetchrow(
                'SELECT review_channel_id FROM guild_settings WHERE guild_id = $1',
                interaction.guild.id,
            )
            target = 채널 or (
                interaction.guild.get_channel(settings['review_channel_id'])
                if settings and settings['review_channel_id'] else None
            ) or interaction.channel
            await conn.execute("""
                INSERT INTO guild_settings (guild_id, review_channel_id, review_auto_message)
                VALUES ($1, $2, $3)
                ON CONFLICT (guild_id) DO UPDATE
                SET review_channel_id = $2, review_auto_message = $3
            """, interaction.guild.id, target.id, REVIEW_GUIDE)
            await replace_review_guide_message(
                conn,
                interaction.client.http,
                interaction.guild.id,
                target.id,
                REVIEW_GUIDE,
            )
    except Exception:
        logging.exception("후기 안내 설정/전송 실패: guild=%s", interaction.guild.id)
        await interaction.followup.send("⚠️ 후기 안내 전송에 실패했습니다. 채널 권한과 데이터베이스 상태를 확인해 주세요.", ephemeral=True)
        return
    await interaction.followup.send(
        f"✅ {target.mention}에 후기 안내를 설정했습니다. 새 후기가 등록되면 기존 안내를 지우고 맨 아래에 다시 전송합니다.",
        ephemeral=True,
    )


class UserInfoModal(discord.ui.Modal):
    def __init__(self, existing_data=None):
        title_str = "📦 배송 정보 수정" if existing_data else "📦 배송 정보 등록"
        super().__init__(title=title_str)
        
        default_name = existing_data['name'] if existing_data else ''
        self.user_name = discord.ui.TextInput(
            label='이름 (필수)', 
            placeholder='홍길동',
            default=default_name, 
            required=True
        )
        
        default_contact = existing_data['contact'] if existing_data else ''
        self.contact = discord.ui.TextInput(
            label='연락처 (필수)', 
            placeholder='010-1234-5678',
            default=default_contact, 
            required=True
        )
        
        default_address = existing_data['address'] if existing_data else ''
        self.address = discord.ui.TextInput(
            label='주소 (필수)', 
            placeholder='편의점택배라면 X 라고 적어주세요.',
            default=default_address, 
            required=True
        )
        
        default_cvs = existing_data['cvs'] if existing_data else ''
        self.cvs = discord.ui.TextInput(
            label='편의점 (필수)', 
            placeholder='일반택배라면 X 라고 적어주세요.',
            default=default_cvs, 
            required=True
        )
        
        self.add_item(self.user_name)
        self.add_item(self.contact)
        self.add_item(self.address)
        self.add_item(self.cvs)

    async def on_submit(self, interaction: discord.Interaction):
        async with bot.db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO user_info (user_id, name, contact, address, cvs) 
                VALUES ($1, $2, $3, $4, $5) 
                ON CONFLICT (user_id) DO UPDATE 
                SET name = $2, contact = $3, address = $4, cvs = $5;
            ''', interaction.user.id, self.user_name.value, self.contact.value, self.address.value, self.cvs.value)
        
        await interaction.response.send_message("✅ 배송 정보가 안전하게 저장 및 수정되었습니다!", ephemeral=True)


@bot.tree.command(name="정보패널", description="배송 정보 등록 및 관리 패널을 생성합니다.")
async def send_info_panel(interaction: discord.Interaction):
    info_content = (
        "## VAPE SP0T USER INFO\n\n"
        "구매를 위해 정보를 입력해 주시기 바랍니다.\n"
        "등록된 정보는 배송 및 구매 처리에 사용됩니다.\n\n"
        "- 🕒 빠른 배송을 위한 정보 등록 절차입니다."
    )
    
    info_payload = {
        "flags": 1 << 15,
        "components": [
            {
                "type": 17, 
                "accent_color": 0x32CD32,
                "components": [
                    {
                        "type": 10,
                        "content": info_content
                    },
                    {"type": 14, "divider": True, "spacing": 1},
                    {"type": 10, "content": "**등록 정보 안내**\n\n`👤` 이름\n`📞` 연락처\n`🏠` 주소\n`🏪` 편의점명"},
                    {"type": 14, "divider": True, "spacing": 1},
                    {"type": 10, "content": "등록된 정보는 구매를 위해 티켓을 여셨을 때 자동으로 표시됩니다.\n매번 이름·연락처·편의점명(또는 주소)을 따로 말씀하실 필요가 없습니다."},
                    {"type": 14, "divider": True, "spacing": 1},
                    {"type": 10, "content": "🏪 편의점 주소를 확인하려면 아래에서 **GS25 또는 CU**를 선택해 주세요."},
                    brand_selector(),
                    {"type": 14, "divider": True, "spacing": 1},
                    {"type": 10, "content": "🌟 아래 버튼을 눌러 정보를 등록해 주세요.\n잘못 입력한 정보는 **정보 수정**으로 변경하고, **정보 조회**에서 배송 정보와 현재 보유 포인트를 확인할 수 있습니다."},
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 2,
                                "style": 1,
                                "label": "정보 등록",
                                "custom_id": "info_register"
                            },
                            {
                                "type": 2,
                                "style": 2,
                                "label": "정보 수정",
                                "custom_id": "info_edit"
                            },
                            {
                                "type": 2,
                                "style": 3,
                                "label": "정보 조회",
                                "custom_id": "info_view"
                            }
                        ]
                    }
                ]
            }
        ]
    }
    
    await interaction.client.http.request(
        discord.http.Route("POST", f"/interactions/{interaction.id}/{interaction.token}/callback"),
        json={"type": 4, "data": info_payload}
    )


@bot.tree.command(name="포인트조회패널", description="유저가 본인의 현재 포인트를 확인할 패널을 생성합니다. (관리자 전용)")
async def send_points_lookup_panel(interaction: discord.Interaction):
    payload = create_v2_payload(
        "## 💰 포인트 조회\n\n아래 **내 포인트 조회** 버튼을 눌러 현재 보유 포인트를 확인해 주세요.\n조회 결과는 본인에게만 표시됩니다.",
        extra_components=[{"type": 1, "components": [
            {"type": 2, "style": 3, "label": "내 포인트 조회", "emoji": {"name": "💰"},
             "custom_id": "point_balance_view"}]}]
    )
    await interaction.client.http.request(
        discord.http.Route("POST", f"/interactions/{interaction.id}/{interaction.token}/callback"),
        json={"type": 4, "data": payload}
    )


async def show_my_points(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("❌ 서버에서 포인트를 조회해 주세요.", ephemeral=True)
        return
    if bot.db_pool is None:
        await interaction.response.send_message("❌ DB가 연결되지 않았습니다.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with bot.db_pool.acquire() as conn:
        balance = await get_balance(conn, interaction.guild.id, interaction.user.id)
    await interaction.followup.send(
        f"## 💰 내 포인트 조회\n\n현재 보유 포인트: **{balance:,}P**",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
    )


@bot.tree.command(name="편의점주소", description="GS25와 CU의 편의점 주소 조회 패널을 생성합니다.")
async def send_store_lookup_panel(interaction: discord.Interaction):
    await interaction.client.http.request(
        discord.http.Route("POST", f"/interactions/{interaction.id}/{interaction.token}/callback"),
        json={"type": 4, "data": lookup_panel()}
    )


@bot.tree.command(name="채팅포인트채널", description="채팅 활동 포인트를 지급할 채널을 설정합니다. (관리자 전용)")
@app_commands.describe(채널="포인트를 적립할 텍스트 채널. 생략하면 현재 채널", 활성화="끄면 채팅 포인트 적립을 중지합니다.", 지급주기분="랜덤 1명에게 지급할 주기 (1~1440분, 기본 1분)")
async def set_chat_points_channel(interaction: discord.Interaction, 채널: discord.TextChannel = None, 활성화: bool = True,
                                  지급주기분: app_commands.Range[int, 1, 1440] = 1):
    await configure_chat_points(interaction, 채널, 활성화, 지급주기분)


@bot.tree.command(name="지급로그", description="채팅 이벤트의 포인트·랜덤박스 지급로그 채널을 설정합니다. (관리자 전용)")
@app_commands.describe(채널="채팅 이벤트 지급로그를 보낼 텍스트 채널")
async def set_reward_log_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    await configure_reward_log(interaction, 채널)


@bot.tree.command(name="유저정보", description="특정 유저가 등록한 배송 및 구매 정보를 조회합니다.")
@app_commands.describe(유저="정보를 조회할 대상을 선택하세요.")
async def view_user_info(interaction: discord.Interaction, 유저: discord.Member):
    if not bot.db_pool: 
        await interaction.response.send_message("❌ DB가 연결되지 않았습니다.", ephemeral=True)
        return



    async with bot.db_pool.acquire() as conn:
        user_data = await conn.fetchrow('SELECT * FROM user_info WHERE user_id = $1', 유저.id)
        point_balance = await get_balance(conn, interaction.guild.id, 유저.id)
        
    if not user_data:
        user_data = {"name": "미등록", "contact": "미등록", "address": "미등록",
                     "cvs": "미등록", "total_spent": 0}
        
    total_spent = user_data['total_spent'] if user_data['total_spent'] else 0
    
    view_content = (
        f"## 📋 {유저.name}님의 등록 정보 조회\n\n"
        "`👤`**이름**\n"
        f"`{user_data['name']}`\n\n"
        "`📞`**연락처**\n"
        f"`{user_data['contact']}`\n\n"
        "`🏠`**주소**\n"
        f"`{user_data['address']}`\n\n"
        "`🏪`**편의점**\n"
        f"`{user_data['cvs']}`\n\n"
        "`💰`**누적 구매액**\n"
        f"`{total_spent:,}원`\n\n"
        "`🪙`**보유 포인트**\n"
        f"`{point_balance:,}P`"
    )
    
    view_payload = {
        "flags": (1 << 15) | (1 << 6), 
        "components": [
            {
                "type": 17, 
                "accent_color": 0x32CD32, 
                "components": [
                    {
                        "type": 10, 
                        "content": view_content
                    }
                ]
            }
        ]
    }
    
    await interaction.client.http.request(
        discord.http.Route("POST", f"/interactions/{interaction.id}/{interaction.token}/callback"), 
        json={"type": 4, "data": view_payload}
    )


# ==========================================
# [기능 4] 구매패널 전송 & 유저운송장 & 메시지
# ==========================================
async def change_user_points(interaction, 유저, amount, remove=False):
    if interaction.guild is None or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ 서버 관리자만 포인트를 변경할 수 있습니다.", ephemeral=True)
        return
    if not bot.db_pool:
        await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("❌ 포인트는 1 이상의 정수로 입력해 주세요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        async with bot.db_pool.acquire() as conn:
            balance = await adjust_points(conn, interaction.guild.id, 유저.id, interaction.user.id,
                                          interaction.id, -amount if remove else amount)
    except PointsError as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)
        return
    action = "제거" if remove else "추가"
    await interaction.followup.send(
        f"✅ {유저.mention}님의 포인트를 {amount:,}P {action}했습니다.\n현재 잔액: {balance:,}P",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="포인트추가", description="선택한 유저에게 포인트를 추가합니다. (관리자 전용)")
@app_commands.describe(유저="포인트를 받을 유저입니다.", 추가할포인트="추가할 포인트를 1 이상의 정수로 입력하세요.")
async def add_user_points(interaction: discord.Interaction, 유저: discord.Member, 추가할포인트: app_commands.Range[int, 1]):
    await change_user_points(interaction, 유저, 추가할포인트)


@bot.tree.command(name="포인트제거", description="선택한 유저의 포인트를 제거합니다. (관리자 전용)")
@app_commands.describe(유저="포인트를 제거할 유저입니다.", 제거할포인트="제거할 포인트를 1 이상의 정수로 입력하세요.")
async def remove_user_points(interaction: discord.Interaction, 유저: discord.Member, 제거할포인트: app_commands.Range[int, 1]):
    await change_user_points(interaction, 유저, 제거할포인트, remove=True)


@bot.tree.command(name="구매패널", description="구매 패널을 생성합니다.")
@app_commands.describe(포인트사용가능="이 주문의 포인트 사용 허용 여부입니다. 허용하면 500~2,000P를 사용할 수 있습니다.")
async def send_purchase_panel(interaction: discord.Interaction, 구매자: discord.Member, 입금금액: str, 상품: str, 수량: str, 포인트사용가능: bool = True):
    if not bot.db_pool: 
        await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
        return

    try:
        입금금액 = f"{parse_amount(입금금액):,}원"
    except PointsError as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        return

    def get_rand(length):
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))
    
    order_id = f"{get_rand(5)}-{get_rand(4)}-{get_rand(5)}"
    
    async with bot.db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO orders (order_id, guild_id, original_channel_id, buyer_id, product, quantity, amount, status, points_allowed)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9);
        ''', order_id, interaction.guild.id, interaction.channel.id, 구매자.id, 상품, 수량, 입금금액, 'PENDING', 포인트사용가능)

    # 구매로그를 제외한 주문 화면에는 실제 구매자를 표시합니다.
    display_buyer = 구매자.mention
        
    # 여러 상품 쉼표(,) 입력 시 줄바꿈 처리
    product_list = [p.strip() for p in 상품.split(",") if p.strip()]
    formatted_product = "\n".join([f"`{p}`" for p in product_list])
    
    panel_content = (
        "## VAPE SP0T ORDER\n\n"
        f"{display_buyer}님의 주문이 접수되었습니다.\n"
        "`아래 정보를 확인하신 후 입금을 진행해 주세요.`\n\n"
        "`🧾`주문정보\n\n"
        "`◾`**주문번호**\n"
        f"`{order_id}`\n\n"
        "`📦`**상품**\n"
        f"{formatted_product}\n\n"
        "`◾`**수량**\n"
        f"`{수량}`\n\n"
        "`💰`**금액**\n"
        f"`{입금금액}`\n\n"
        f"{'포인트 사용 가능 (500~2,000P · 1P = 1원)' if 포인트사용가능 else '포인트사용 불가 상품'}\n\n"
        "`⚠️`입금 전 확인해 주세요\n"
        "- 입금자명이 맞는지 정확하게 확인해 주시기 바랍니다.\n"
        "- 주문하신 금액과 일치하게 입금해 주시기 바랍니다.\n"
        "- 입금 실수로 인한 책임은 지지 않습니다."
    )
    
    purchase_payload = {
        "flags": 1 << 15,
        "allowed_mentions": {"parse": [], "users": [str(구매자.id)]},
        "components": [
            {
                "type": 17, 
                "accent_color": 0x32CD32,
                "components": [
                    {
                        "type": 10, 
                        "content": panel_content
                    }, 
                    {
                        "type": 1, 
                        "components": [
                            {
                                "type": 2, 
                                "style": 1, 
                                "label": "결제하기", 
                                "custom_id": f"pay_{order_id}"
                            }
                        ]
                    }
                ]
            }
        ]
    }
    
    await interaction.client.http.request(
        discord.http.Route("POST", f"/interactions/{interaction.id}/{interaction.token}/callback"),
        json={"type": 4, "data": purchase_payload}
    )


# ---------------------------------------------------------
# 입금자명 입력 모달 
# ---------------------------------------------------------
class DepositModal(discord.ui.Modal):
    def __init__(self, order_id: str, order_data: dict, settings_data: dict, point_balance: int = 0):
        super().__init__(title="💳 입금 확인")
        self.order_id = order_id
        self.order_data = order_data
        self.settings_data = settings_data
        self.points_allowed = order_data.get('points_allowed', True)
        maximum = maximum_points(point_balance, parse_amount(order_data['amount']), self.points_allowed)
        self.depositor_name = discord.ui.TextInput(
            label="입금자명", placeholder="실제 입금하시는 분의 성함을 입력해 주세요.", required=True, max_length=50,
        )
        self.points = discord.ui.TextInput(
            label=f"사용할 포인트 (최대 {maximum:,}P)" if self.points_allowed else "사용할 포인트",
            placeholder="500~2,000P 사용 · 사용하지 않으려면 0" if self.points_allowed else "포인트사용 불가 상품",
            default=str(maximum) if self.points_allowed else "포인트사용 불가 상품",
            required=self.points_allowed, max_length=19 if self.points_allowed else 100,
        )
        self.add_item(self.depositor_name)
        self.add_item(self.points)

    async def on_submit(self, interaction: discord.Interaction):
        if not bot.db_pool:
            await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
            return
        if interaction.guild is None or interaction.guild.id != self.order_data['guild_id'] or interaction.user.id != self.order_data['buyer_id']:
            await interaction.response.send_message("❌ 이 주문의 구매자만 결제할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            points = parse_points(self.points.value) if self.points_allowed else 0
            async with bot.db_pool.acquire() as conn:
                order = await request_payment(conn, self.order_id, interaction.guild.id, interaction.user.id,
                                              self.depositor_name.value, points)
        except PointsError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        name = safe_text(order['depositor_name'])
        formatted_product = "\n".join(f"`{p.strip()}`" for p in order['product'].split(",") if p.strip())
        summary = payment_summary(order)
        admin_req = (
            "## 🔔 새로운 결제 확인 요청\n\n"
            f"`👤`**구매자**\n<@{order['buyer_id']}>\n\n"
            f"`✍️`**입금자명**\n`{name}`\n\n"
            f"`◾`**주문번호**\n`{self.order_id}`\n\n"
            f"`📦`**상품**\n{formatted_product}\n\n"
            f"`◾`**수량**\n`{order['quantity']}`\n\n"
            f"`💰`**결제금액**\n{summary}"
        )
        admin_payload = create_v2_payload(admin_req, extra_components=[{
            "type": 1, "components": [
                {"type": 2, "style": 3, "label": "승인", "custom_id": f"approve_{self.order_id}"},
                {"type": 2, "style": 4, "label": "거절", "custom_id": f"reject_{self.order_id}"},
            ],
        }])
        admin_payload['allowed_mentions'] = {"parse": []}
        try:
            await interaction.client.http.request(
                discord.http.Route("POST", f"/channels/{self.settings_data['approval_channel_id']}/messages"), json=admin_payload,
            )
        except discord.HTTPException:
            async with bot.db_pool.acquire() as conn:
                await reset_failed_payment(conn, self.order_id, interaction.guild.id)
            await interaction.followup.send("❌ 관리자에게 결제 요청을 보내지 못해 포인트 사용을 취소했습니다. 채널 권한을 확인한 후 다시 시도해 주세요.", ephemeral=True)
            return

        bank_name = self.settings_data.get('bank_name') or "토스뱅크"
        account_number = self.settings_data.get('account_number') or "1002-5778-3501"
        account_holder = self.settings_data.get('account_holder') or "문*서"
        instructions = "포인트로 전액 결제되어 입금할 금액이 없습니다. 관리자 승인을 기다려 주세요." if order['cash_amount'] == 0 else "아래 계좌로 실제 입금금액을 입금해 주세요."
        pay_info = (
            f"## 결제 안내\n\n{instructions}\n\n"
            f"`💰`계좌 정보\n- 은행\n`{bank_name}`\n\n"
            f"- 계좌번호\n`{account_number}`\n\n- 예금주\n`{account_holder}`\n\n"
            f"`📦`**상품**\n{formatted_product}\n\n"
            f"`◾`**수량**\n`{order['quantity']}`\n\n"
            f"`✍️`**입금자명**\n`{name}`\n\n"
            f"`💰`**결제금액**\n{summary}\n\n"
            "- 입력한 입금자명과 실제 입금액을 확인해 주세요."
        )
        pay_payload = create_v2_payload(pay_info, ephemeral=True)
        pay_payload['allowed_mentions'] = {"parse": []}
        await interaction.client.http.request(
            discord.http.Route("POST", f"/webhooks/{interaction.application_id}/{interaction.token}"), json=pay_payload,
        )


@bot.tree.command(name="운송장", description="구매 티켓과 구매자 DM으로 운송장 발급 안내를 전송합니다. (관리자 전용)")
@app_commands.describe(
    유저="운송장을 발급받을 구매자를 선택해 주세요.",
    주문번호="해당 구매자의 주문번호를 입력해 주세요.",
    운송장번호="발급된 운송장 번호를 입력해 주세요."
)
async def issue_waybill(
    interaction: discord.Interaction,
    유저: discord.Member,
    주문번호: str,
    운송장번호: str,
):
    if not bot.db_pool:
        await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
        return

    if interaction.guild is None or not interaction.channel:
        await interaction.response.send_message("❌ 서버의 구매 티켓 채널에서 사용해 주세요.", ephemeral=True)
        return

    try:
        waybill_number = normalize_waybill(운송장번호)
    except DeliveryTrackingError as error:
        await interaction.response.send_message(f"❌ {error}", ephemeral=True)
        return

    order_id = 주문번호.strip()
    if not order_id:
        await interaction.response.send_message("❌ 주문번호를 입력해 주세요.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    async with bot.db_pool.acquire() as conn:
        ticket = await conn.fetchrow(
            'SELECT user_id, ticket_type FROM tickets WHERE channel_id = $1',
            interaction.channel.id,
        )
        order = await conn.fetchrow(
            """
            SELECT order_id FROM orders
            WHERE order_id = $1 AND guild_id = $2 AND buyer_id = $3
            """,
            order_id,
            interaction.guild.id,
            유저.id,
        )

    if not ticket or ticket['ticket_type'] != 'purchase':
        await interaction.followup.send("❌ 구매문의 티켓 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return

    if ticket['user_id'] != 유저.id:
        await interaction.followup.send("❌ 이 티켓을 연 유저와 선택한 유저가 일치하지 않습니다.", ephemeral=True)
        return

    if not order:
        await interaction.followup.send(
            f"❌ {유저.mention}님의 주문 중 `{order_id}` 주문번호를 찾을 수 없습니다.",
            ephemeral=True,
        )
        return

    message = (
        f"**- {유저.mention} 귀하의 운송장이 발급되었습니다.\n"
        f"- `◼️` 운송장 : `{waybill_number}`\n"
        f"- `◼️` 주문번호 : `{order_id}`\n"
        "> 발급받으신 운송장은 <#1553664932485013594> 채널에서 가능 합니다. **"
    )
    payload = create_v2_payload(message)
    payload['allowed_mentions'] = {"parse": [], "users": [str(유저.id)]}

    try:
        await interaction.client.http.request(
            discord.http.Route("POST", f"/channels/{interaction.channel.id}/messages"),
            json=payload,
        )
    except discord.HTTPException:
        await interaction.followup.send("❌ 티켓 채널에 운송장 안내를 전송하지 못했습니다. 채널 권한을 확인해 주세요.", ephemeral=True)
        return

    try:
        dm_channel = await 유저.create_dm()
        await interaction.client.http.request(
            discord.http.Route("POST", f"/channels/{dm_channel.id}/messages"),
            json=payload,
        )
        await interaction.followup.send(
            f"✅ 티켓 채널과 {유저.mention}님의 DM으로 운송장 발급 안내를 전송했습니다.",
            ephemeral=True,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            f"⚠️ 티켓 채널에는 전송했지만 {유저.mention}님이 DM을 받을 수 없어 DM 전송에 실패했습니다.",
            ephemeral=True,
        )
    except discord.HTTPException:
        await interaction.followup.send(
            f"⚠️ 티켓 채널에는 전송했지만 {유저.mention}님의 DM 전송에 실패했습니다.",
            ephemeral=True,
        )


@bot.tree.command(name="메시지", description="봇이 대신해서 메시지를 전송해 줍니다.")
@app_commands.describe(내용="전송할 내용을 입력해 주세요. (미리 복사해 붙여넣거나 \\n 입력 시 줄바꿈 지원)")
async def send_custom_message(interaction: discord.Interaction, 내용: str):
    real_content = 내용.replace('\\n', '\n') 
    await interaction.response.send_message("✅ 봇이 대신 메시지를 전송했습니다.", ephemeral=True)
    await interaction.channel.send(real_content)


# ---------------------------------------------------------
# [출퇴근 시스템]
# ---------------------------------------------------------
@bot.tree.command(name="출퇴근알림", description="출근·퇴근·외출·취침·기타 퇴근 로그 채널을 설정합니다. (관리자 전용)")
async def set_clock_log_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ 관리자만 설정할 수 있습니다.", ephemeral=True)
        return
    await update_setting(interaction, "clock_log_channel", 채널.id, f"✅ 출퇴근 알림 채널이 {채널.mention}(으)로 설정되었습니다.")

@bot.tree.command(name="출퇴근음성", description="출근 및 퇴근 시 봇이 접속할 음성 채널을 설정합니다. (관리자 전용)")
async def set_clock_vc(interaction: discord.Interaction, 출근채널: discord.VoiceChannel, 퇴근채널: discord.VoiceChannel):
    if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ 관리자만 설정할 수 있습니다.", ephemeral=True)
        return
    if not bot.db_pool:
        await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
        return
        
    async with bot.db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO guild_settings (guild_id, clock_in_vc, clock_out_vc) 
            VALUES ($1, $2, $3) 
            ON CONFLICT (guild_id) DO UPDATE SET clock_in_vc = $2, clock_out_vc = $3;
        ''', interaction.guild.id, 출근채널.id, 퇴근채널.id)
        
    await interaction.response.send_message(f"✅ 출근 음성 채널: {출근채널.mention}\n✅ 퇴근 음성 채널: {퇴근채널.mention}\n설정이 완료되었습니다.", ephemeral=True)


def clock_reason_text(value: str) -> str:
    """기타 퇴근 사유가 코드블록이나 추가 멘션을 만들지 않도록 표시용으로 정리합니다."""
    return value.strip().replace("`", "ˋ").replace("@", "@\u200b")


async def process_clock_action(interaction: discord.Interaction, custom_id: str, reason: str | None = None):
    if not bot.db_pool:
        await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
        return

    clock_actions = {
        "clock_in": ("출근", "🏢", 0x32CD32, "clock_in_vc"),
        "clock_out": ("퇴근", "🏠", 0xFF0000, "clock_out_vc"),
        "clock_away": ("외출", "🚶", 0x3498DB, None),
        "clock_sleep": ("취침", "😴", 0x5865F2, None),
        "clock_other": ("기타 퇴근", "📝", 0x95A5A6, "clock_out_vc"),
    }
    if custom_id not in clock_actions:
        await interaction.response.send_message("❌ 알 수 없는 출퇴근 상태입니다.", ephemeral=True)
        return

    cleaned_reason = clock_reason_text(reason or "")
    if custom_id == "clock_other" and not cleaned_reason:
        await interaction.response.send_message("❌ 퇴근 이유를 입력해 주세요.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    async with bot.db_pool.acquire() as conn:
        settings = await conn.fetchrow(
            "SELECT * FROM guild_settings WHERE guild_id = $1",
            interaction.guild.id,
        )
    if not settings:
        await interaction.followup.send("❌ 서버 설정이 등록되지 않았습니다.", ephemeral=True)
        return

    action_name, action_emoji, log_color, vc_setting = clock_actions[custom_id]
    vc_id = settings[vc_setting] if vc_setting else None
    log_ch_id = settings["clock_log_channel"]

    if vc_id:
        vc = interaction.guild.get_channel(vc_id)
        if vc and isinstance(vc, discord.VoiceChannel):
            try:
                voice_client = interaction.guild.voice_client
                if voice_client:
                    if voice_client.channel.id != vc.id:
                        await voice_client.move_to(vc)
                else:
                    await vc.connect(self_mute=True, self_deaf=True)
            except Exception as exc:
                logging.warning("음성 채널 접속 중 오류: %s", exc)

    if log_ch_id:
        log_ch = interaction.guild.get_channel(log_ch_id)
        if log_ch:
            now_str = datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
            availability = (
                "모든 업무가 가능합니다."
                if custom_id == "clock_in"
                else "모든 업무 확인이 늦을 수 있습니다."
            )
            reason_line = f"`📝` **퇴근 이유:** {cleaned_reason}\n" if cleaned_reason else ""
            log_content = (
                "@everyone\n\n"
                f"## {action_emoji} {action_name} 알림\n\n"
                f"`👤` **유저:** {interaction.user.mention}\n"
                f"`🕒` **시간:** `{now_str}`\n"
                f"{reason_line}\n"
                f"> **{availability}**"
            )
            log_payload = create_v2_payload(log_content, color=log_color)
            log_payload["allowed_mentions"] = {"parse": ["everyone"]}
            try:
                await interaction.client.http.request(
                    discord.http.Route("POST", f"/channels/{log_ch.id}/messages"),
                    json=log_payload,
                )
            except discord.HTTPException:
                logging.exception("출퇴근 알림 전송 실패: guild=%s", interaction.guild.id)

    await interaction.followup.send(f"✅ {action_name} 처리가 완료되었습니다.", ephemeral=True)


class OtherClockOutModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="기타 퇴근 사유")
        self.reason = discord.ui.TextInput(
            label="퇴근 이유",
            placeholder="버튼에 없는 퇴근 사유를 입력해 주세요.",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=500,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await process_clock_action(interaction, "clock_other", self.reason.value)

@bot.tree.command(name="출퇴근패널", description="직원 상태를 기록할 수 있는 출퇴근 버튼 패널을 생성합니다. (관리자 전용)")
async def send_clock_panel(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ 관리자만 생성할 수 있습니다.", ephemeral=True)
        return
        
    content = (
        "## 🏢 출퇴근 기록\n\n"
        "- 아래 버튼을 눌러 출근·퇴근·외출·취침·기타 퇴근 상태를 기록해 주세요.\n"
        "- 기록 시 지정된 알림 채널에 로그가 전송됩니다."
    )
    
    extra = [{
        "type": 1,
        "components": [
            {
                "type": 2,
                "style": 3,
                "label": "출근하기",
                "custom_id": "clock_in",
                "emoji": {"name": "🟢"}
            },
            {
                "type": 2,
                "style": 4,
                "label": "퇴근하기",
                "custom_id": "clock_out",
                "emoji": {"name": "🔴"}
            },
            {
                "type": 2,
                "style": 1,
                "label": "외출하기",
                "custom_id": "clock_away",
                "emoji": {"name": "🚶"}
            },
            {
                "type": 2,
                "style": 2,
                "label": "취침하기",
                "custom_id": "clock_sleep",
                "emoji": {"name": "😴"}
            },
            {
                "type": 2,
                "style": 2,
                "label": "기타 퇴근",
                "custom_id": "clock_other",
                "emoji": {"name": "📝"}
            }
        ]
    }]
    
    await interaction.client.http.request(
        discord.http.Route("POST", f"/interactions/{interaction.id}/{interaction.token}/callback"),
        json={"type": 4, "data": create_v2_payload(content, extra_components=extra)}
    )


# ---------------------------------------------------------
# [주문내역 및 주문취소 기능]
# ---------------------------------------------------------
class OrderHistoryView(discord.ui.View):
    def __init__(self, orders, target_user=None):
        super().__init__(timeout=180)
        self.orders = orders
        self.target_user = target_user
        self.current_page = 0
        self.items_per_page = 5
        self.max_pages = max(1, (len(orders) + self.items_per_page - 1) // self.items_per_page)

    def generate_embed(self):
        embed = discord.Embed(title="📦 주문 내역 검색 결과", color=0x32CD32)
        
        if self.target_user:
            embed.description = f"{self.target_user.mention}님의 주문 내역입니다.\n\n"
        else:
            embed.description = "서버 전체 주문 내역입니다.\n\n"

        start_idx = self.current_page * self.items_per_page
        end_idx = start_idx + self.items_per_page
        page_orders = self.orders[start_idx:end_idx]

        for order in page_orders:
            if order['status'] == 'APPROVED':
                status_str = "✅ 승인완료"
            elif order['status'] == 'REJECTED':
                status_str = "❌ 거절됨"
            else:
                status_str = "⏳ 대기중"
                
            if order['created_at']:
                kst_time = order['created_at'].astimezone(KST) if order['created_at'].tzinfo else order['created_at'].replace(tzinfo=datetime.timezone.utc).astimezone(KST)
                date_str = kst_time.strftime("%Y-%m-%d %H:%M")
            else:
                date_str = "시간 정보 없음"
                
            product_str = order['product'].replace('\n', ', ')
            
            field_name = f"[{date_str}] 🧾 {order['order_id']}"
            field_value = (
                f"👤 **구매자:** <@{order['buyer_id']}>\n"
                f"📦 **상품:** {product_str}\n"
                f"💰 **금액:** {order['amount']}\n"
                f"📌 **상태:** {status_str}"
            )
            embed.add_field(name=field_name, value=field_value, inline=False)
        
        embed.set_footer(text=f"페이지 {self.current_page + 1} / {self.max_pages} (총 {len(self.orders)}건)")
        return embed

    @discord.ui.button(label="이전 페이지", style=discord.ButtonStyle.primary, custom_id="prev_page")
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await interaction.response.edit_message(embed=self.generate_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="다음 페이지", style=discord.ButtonStyle.primary, custom_id="next_page")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.max_pages - 1:
            self.current_page += 1
            await interaction.response.edit_message(embed=self.generate_embed(), view=self)
        else:
            await interaction.response.defer()


@bot.tree.command(name="주문내역", description="모든 주문 내역 또는 특정 유저의 주문 내역을 확인합니다. (관리자 전용)")
@app_commands.describe(유저="조회할 유저를 선택해 주세요. 비워두면 전체 주문이 조회됩니다.")
async def view_order_history(interaction: discord.Interaction, 유저: discord.Member = None):
    if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ 관리자만 열람할 수 있습니다.", ephemeral=True)
        return
        
    if not bot.db_pool:
        await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
        return
        
    async with bot.db_pool.acquire() as conn:
        if 유저:
            orders = await conn.fetch('SELECT * FROM orders WHERE guild_id = $1 AND buyer_id = $2 ORDER BY created_at DESC', interaction.guild.id, 유저.id)
        else:
            orders = await conn.fetch('SELECT * FROM orders WHERE guild_id = $1 ORDER BY created_at DESC', interaction.guild.id)
            
    if not orders:
        await interaction.response.send_message("❌ 조회된 주문 내역이 없습니다.", ephemeral=True)
        return
        
    view = OrderHistoryView(orders, 유저)
    await interaction.response.send_message(embed=view.generate_embed(), view=view, ephemeral=True)


@bot.tree.command(name="주문취소", description="특정 주문을 삭제하고 누적 금액 및 등급을 롤백합니다. (관리자 전용)")
@app_commands.describe(주문번호="취소 및 삭제할 주문번호를 정확히 입력해 주세요.")
async def cancel_order(interaction: discord.Interaction, 주문번호: str):
    if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ 관리자만 주문을 취소할 수 있습니다.", ephemeral=True)
        return
        
    if not bot.db_pool:
        await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=True)
    
    async with bot.db_pool.acquire() as conn:
        try:
            order = await cancel_order_record(conn, 주문번호, interaction.guild.id)
        except PointsError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
            
        buyer_id = order['buyer_id']
        
        if order['status'] == 'APPROVED':
            user_info_row = await conn.fetchrow('SELECT total_spent FROM user_info WHERE user_id = $1', buyer_id)
            current_total = user_info_row['total_spent'] if user_info_row else 0
            
            member = interaction.guild.get_member(buyer_id)
            settings = await conn.fetchrow('SELECT buyer_role_id FROM guild_settings WHERE guild_id = $1', interaction.guild.id)
            tiers = await conn.fetch('SELECT role_id, required_amount FROM vip_tiers WHERE guild_id = $1 ORDER BY required_amount DESC', interaction.guild.id)
            
            if member:
                if current_total < 1 and settings and settings.get('buyer_role_id'):
                    b_role = interaction.guild.get_role(settings['buyer_role_id'])
                    if b_role and b_role in member.roles:
                        try:
                            await member.remove_roles(b_role)
                        except Exception:
                            pass
                            
                target_role_id = None
                for t in tiers:
                    if current_total >= t['required_amount']:
                        target_role_id = t['role_id']
                        break
                        
                tier_role_ids = [t['role_id'] for t in tiers]
                for role_id in tier_role_ids:
                    if role_id != target_role_id:
                        role_to_remove = interaction.guild.get_role(role_id)
                        if role_to_remove and role_to_remove in member.roles:
                            try:
                                await member.remove_roles(role_to_remove)
                            except Exception:
                                pass
                                
                if target_role_id:
                    target_role = interaction.guild.get_role(target_role_id)
                    if target_role and target_role not in member.roles:
                        try:
                            await member.add_roles(target_role)
                        except Exception:
                            pass
                            
        
    await update_leaderboard(interaction.guild.id)
    
    await interaction.followup.send(f"✅ `{주문번호}` 주문이 삭제(취소)되었으며, 해당 유저의 누적 금액 및 역할이 정상적으로 롤백되었습니다.", ephemeral=True)


# ==========================================
# [기능 5] 문의 티켓 시스템 (모달 및 생성)
# ==========================================
PURCHASE_INFO_FIELDS = ("name", "contact", "address", "cvs")


def has_complete_purchase_info(user_data) -> bool:
    """구매 배송에 필요한 정보가 모두 등록되어 있는지 확인합니다."""
    if not user_data:
        return False

    try:
        return all(str(user_data[field] or "").strip() for field in PURCHASE_INFO_FIELDS)
    except (KeyError, TypeError):
        return False


def build_missing_purchase_info_notice(user_id: int) -> str:
    return (
        "## ⚠️ 구매자 정보 미등록\n\n"
        f"<@{user_id}>님, 아직 배송 정보가 등록되지 않았습니다!\n"
        "빠른 발송을 위해 채널 밖에서 <#1553664818349608960> 채널에서 [정보 등록] 버튼을 눌러 배송지를 등록해 주세요."
    )


async def create_ticket_channel(interaction: discord.Interaction, t_type: str, type_kr: str, form_answers: dict):
    await interaction.response.defer(ephemeral=True)
    purchase_info_missing = False
    
    async with bot.db_pool.acquire() as conn:
        settings = await conn.fetchrow('SELECT * FROM guild_settings WHERE guild_id = $1', interaction.guild.id)
        
        if not settings or not settings[f"ticket_cat_{t_type}"]: 
            await interaction.followup.send("❌ 카테고리가 설정되지 않았습니다.", ephemeral=True)
            return
            
        category = interaction.guild.get_channel(settings[f"ticket_cat_{t_type}"])
        ticket_count = await conn.fetchval('SELECT COUNT(*) FROM tickets')
        ticket_id_str = f"{ticket_count + 1:04d}"
        channel_name = f"{type_kr}-{interaction.user.name}-{ticket_id_str}"
        
        overwrites = category.overwrites
        overwrites[interaction.user] = discord.PermissionOverwrite(
            view_channel=True, 
            read_messages=True, 
            send_messages=True
        )
        
        ticket_channel = await interaction.guild.create_text_channel(
            name=channel_name, 
            category=category, 
            overwrites=overwrites
        )
        
        await conn.execute('''
            INSERT INTO tickets (channel_id, user_id, ticket_type) 
            VALUES ($1, $2, $3)
        ''', ticket_channel.id, interaction.user.id, t_type)

        answers_text = ""
        for key, value in form_answers.items():
            answers_text += f"`◾`**{key}**\n`{value}`\n\n"

        welcome_content = (
            f"## 🎫 {type_kr} 티켓이 생성되었습니다\n\n"
            f"{interaction.user.mention}님, 문의하실 내용을 남겨주시면 관리자가 확인 후 답변해 드립니다.\n\n"
            "### 📝 작성된 내용\n\n"
            f"{answers_text.strip()}"
        )
        
        welcome_payload = {
            "flags": 1 << 15,
            "components": [
                {
                    "type": 17, 
                    "accent_color": 0x32CD32, 
                    "components": [
                        {
                            "type": 10, 
                            "content": welcome_content
                        }, 
                        {
                            "type": 1, 
                            "components": [
                                {
                                    "type": 2, 
                                    "style": 4, 
                                    "label": "티켓 닫기", 
                                    "custom_id": "ticket_close"
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        
        await interaction.client.http.request(
            discord.http.Route("POST", f"/channels/{ticket_channel.id}/messages"), 
            json=welcome_payload
        )
        
        if t_type == "purchase":
            user_data = await conn.fetchrow('SELECT * FROM user_info WHERE user_id = $1', interaction.user.id)
            
            if has_complete_purchase_info(user_data):
                info_content = (
                    "## 📋 구매자 등록 배송 정보\n\n"
                    "`👤`**이름:** `" + user_data['name'] + "`\n\n"
                    "`📞`**연락처:** `" + user_data['contact'] + "`\n\n"
                    "`🏠`**주소:** `" + user_data['address'] + "`\n\n"
                    "`🏪`**편의점:** `" + user_data['cvs'] + "`\n\n"
                    "-# 💡 관리자는 위 정보를 바탕으로 상품을 발송합니다."
                )
                info_payload = {
                    "flags": 1 << 15,
                    "components": [
                        {
                            "type": 17,
                            "accent_color": 0x32CD32,
                            "components": [
                                {
                                    "type": 10,
                                    "content": info_content
                                }
                            ]
                        }
                    ]
                }

                await interaction.client.http.request(
                    discord.http.Route("POST", f"/channels/{ticket_channel.id}/messages"),
                    json=info_payload
                )
            else:
                purchase_info_missing = True
                
        await interaction.followup.send(f"✅ 티켓이 정상적으로 생성되었습니다: {ticket_channel.mention}", ephemeral=True)

        if purchase_info_missing:
            await interaction.followup.send(
                build_missing_purchase_info_notice(interaction.user.id),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
            )


class PurchaseTicketModal(discord.ui.Modal, title="💳 구매 문의 작성"):
    product = discord.ui.TextInput(
        label="구매상품", 
        required=True
    )
    quantity = discord.ui.TextInput(
        label="수량", 
        placeholder="1", 
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        answers = {
            "구매상품": self.product.value, 
            "수량": self.quantity.value
        }
        await create_ticket_channel(interaction, "purchase", "구매문의", answers)


class GeneralTicketModal(discord.ui.Modal, title="💌 일반 문의 작성"):
    content = discord.ui.TextInput(
        label="문의내용", 
        style=discord.TextStyle.paragraph, 
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        answers = {
            "문의내용": self.content.value
        }
        await create_ticket_channel(interaction, "general", "일반문의", answers)


class PartnerTicketModal(discord.ui.Modal, title="🤝 제휴 문의 작성"):
    server_link = discord.ui.TextInput(
        label="서버링크", 
        required=True
    )
    banner_name = discord.ui.TextInput(
        label="배너명", 
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        answers = {
            "서버링크": self.server_link.value, 
            "배너명": self.banner_name.value
        }
        await create_ticket_channel(interaction, "partner", "제휴문의", answers)


@bot.tree.command(name="티켓카테고리", description="티켓 카테고리를 설정합니다.")
async def set_ticket_cat(interaction: discord.Interaction, 구매문의_카테고리: discord.CategoryChannel, 일반문의_카테고리: discord.CategoryChannel, 제휴문의_카테고리: discord.CategoryChannel):
    if not bot.db_pool: 
        await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
        return
        
    async with bot.db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO guild_settings (guild_id, ticket_cat_purchase, ticket_cat_general, ticket_cat_partner) 
            VALUES ($1, $2, $3, $4) 
            ON CONFLICT (guild_id) DO UPDATE 
            SET ticket_cat_purchase = $2, ticket_cat_general = $3, ticket_cat_partner = $4;
        ''', interaction.guild.id, 구매문의_카테고리.id, 일반문의_카테고리.id, 제휴문의_카테고리.id)
        
    await interaction.response.send_message("✅ 카테고리 설정 완료!", ephemeral=True)


@bot.tree.command(name="보관카테고리", description="닫힌 티켓 보관 카테고리를 설정합니다.")
async def set_archive_cat(interaction: discord.Interaction, 구매문의_보관: discord.CategoryChannel, 일반문의_보관: discord.CategoryChannel, 제휴문의_보관: discord.CategoryChannel):
    if not bot.db_pool: 
        await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
        return
        
    async with bot.db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO guild_settings (guild_id, archive_cat_purchase, archive_cat_general, archive_cat_partner) 
            VALUES ($1, $2, $3, $4) 
            ON CONFLICT (guild_id) DO UPDATE 
            SET archive_cat_purchase = $2, archive_cat_general = $3, archive_cat_partner = $4;
        ''', interaction.guild.id, 구매문의_보관.id, 일반문의_보관.id, 제휴문의_보관.id)
        
    await interaction.response.send_message("✅ 보관 카테고리 설정 완료!", ephemeral=True)


@bot.tree.command(name="티켓로그채널", description="티켓 삭제 시 로그 전송 채널을 설정합니다.")
async def set_ticket_log_chan(interaction: discord.Interaction, 채널: discord.TextChannel):
    await update_setting(interaction, "ticket_log_channel_id", 채널.id, f"✅ 로그 채널 설정이 {채널.mention}(으)로 완료되었습니다.")


PURCHASE_TICKET_STATUSES = ("구매완료", "배송준비중", "배송중", "배송완료")
PURCHASE_TICKET_PREFIXES = ("구매문의",) + PURCHASE_TICKET_STATUSES


def purchase_ticket_status_name(channel_name: str, status: str) -> str:
    if status not in PURCHASE_TICKET_STATUSES:
        raise ValueError("지원하지 않는 티켓 상태입니다.")
    current_prefix, separator, suffix = channel_name.partition("-")
    if not separator or not suffix or current_prefix not in PURCHASE_TICKET_PREFIXES:
        raise ValueError("구매문의 티켓 채널 이름을 확인할 수 없습니다.")
    updated_name = f"{status}-{suffix}"
    if len(updated_name) > 100:
        raise ValueError("변경할 채널 이름이 Discord의 100자 제한을 초과합니다.")
    return updated_name


@bot.tree.command(name="티켓상태", description="현재 구매문의 티켓의 상태를 변경합니다. (관리자 전용)")
@app_commands.describe(상태="채널 이름에 표시할 구매 처리 상태")
@app_commands.choices(상태=[
    app_commands.Choice(name="구매완료", value="구매완료"),
    app_commands.Choice(name="배송준비중", value="배송준비중"),
    app_commands.Choice(name="배송중", value="배송중"),
    app_commands.Choice(name="배송완료", value="배송완료"),
])
async def set_ticket_status(interaction: discord.Interaction, 상태: app_commands.Choice[str]):
    if bot.db_pool is None:
        await interaction.response.send_message("❌ 데이터베이스가 연결되지 않았습니다.", ephemeral=True)
        return

    async with bot.db_pool.acquire() as conn:
        ticket = await conn.fetchrow(
            'SELECT ticket_type FROM tickets WHERE channel_id = $1', interaction.channel.id
        )

    if ticket is None:
        await interaction.response.send_message("❌ 이 명령어는 봇이 생성한 티켓 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if ticket['ticket_type'] != "purchase":
        await interaction.response.send_message("❌ 구매문의 티켓에서만 상태를 변경할 수 있습니다.", ephemeral=True)
        return

    try:
        new_name = purchase_ticket_status_name(interaction.channel.name, 상태.value)
    except ValueError as error:
        await interaction.response.send_message(f"❌ {error}", ephemeral=True)
        return

    if new_name == interaction.channel.name:
        await interaction.response.send_message(f"✅ 이미 **{상태.value}** 상태입니다.", ephemeral=True)
        return

    old_name = interaction.channel.name
    try:
        await interaction.channel.edit(
            name=new_name,
            reason=f"{interaction.user}님이 구매 티켓 상태를 {상태.value}(으)로 변경",
        )
    except discord.Forbidden:
        await interaction.response.send_message("❌ 봇에 채널 관리 권한이 없어 이름을 변경할 수 없습니다.", ephemeral=True)
        return
    except discord.HTTPException:
        await interaction.response.send_message("❌ Discord에서 채널 이름을 변경하지 못했습니다. 잠시 후 다시 시도해 주세요.", ephemeral=True)
        return

    await interaction.response.send_message(
        f"✅ 티켓 상태를 **{상태.value}**(으)로 변경했습니다.\n`{old_name}` → `{new_name}`",
        ephemeral=True,
    )


@bot.tree.command(name="문의패널", description="문의(티켓) 생성 패널을 띄웁니다.")
async def send_support_panel(interaction: discord.Interaction):
    support_content = (
        "## VAPE SP0T SUPPORT\n\n"
        "- 아래에서 문의 유형을 선택하여 티켓을 열어 주시기 바랍니다.\n\n"
        "- `💳`구매문의\n"
        "> 전담, 상단배너 구매를 위한 티켓입니다.\n\n"
        "- `💌`일반문의\n"
        "> 질문 또는 후원 등 기타 문의를 위한 티켓입니다.\n\n"
        "- `🤝`제휴문의\n"
        "> 파트너 또는 제휴상단 문의를 위한 티켓입니다.\n\n"
        "-# 티켓을 여신 후 10분 동안 응답이 없을 경우 티켓이 자동으로 닫힙니다.\n"
        "-# 관리자에게 욕설 및 비방은 삼가 주시기 바랍니다.\n"
        "-# 기본적인 예의를 지켜 주시기 바랍니다."
    )
    
    support_payload = {
        "flags": 1 << 15,
        "components": [
            {
                "type": 17, 
                "accent_color": 0x32CD32,
                "components": [
                    {
                        "type": 10, 
                        "content": support_content
                    },
                    {
                        "type": 1, 
                        "components": [
                            {
                                "type": 3, 
                                "custom_id": "select_ticket_type", 
                                "placeholder": "티켓 유형을 선택해 주세요", 
                                "options": [
                                    {
                                        "label": "구매문의", 
                                        "value": "purchase", 
                                        "description": "전담, 상단배너를 구매하는 티켓입니다.", 
                                        "emoji": {"name": "💳"}
                                    },
                                    {
                                        "label": "일반문의", 
                                        "value": "general", 
                                        "description": "질문 혹은 후원 등 기타 문의를 담당하는 티켓입니다.", 
                                        "emoji": {"name": "💌"}
                                    },
                                    {
                                        "label": "제휴문의", 
                                        "value": "partner", 
                                        "description": "파트너 혹은 제휴상단 문의를 담당하는 티켓입니다.", 
                                        "emoji": {"name": "🤝"}
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
    }
    
    await interaction.client.http.request(
        discord.http.Route("POST", f"/interactions/{interaction.id}/{interaction.token}/callback"), 
        json={"type": 4, "data": support_payload}
    )


# ==========================================
# [신규 업데이트] 실시간 인기 기기 & 액상 자동 갱신 패널 시스템
# ==========================================
# 10분마다 인기 기기 및 액상 패널을 갱신하는 함수
async def update_popular_panels(guild_id: int):
    if not bot.db_pool: 
        return
        
    async with bot.db_pool.acquire() as conn:
        settings = await conn.fetchrow('SELECT * FROM guild_settings WHERE guild_id = $1', guild_id)
        if not settings: 
            return
            
    now_kst = datetime.datetime.now(KST)
    update_time = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    date_str = update_time.strftime("%Y-%m-%d %H:%M:%S")
    
    headers = {
        "Authorization": f"Bot {os.environ.get('BOT_TOKEN')}", 
        "Content-Type": "application/json"
    }
    
    # 기기 패널 업데이트 (비비빈스 & 일렉샵 인기 트렌드 기준)
    if settings['pop_device_channel_id'] and settings['pop_device_message_id']:
        top_devices = [
            "헬베이프 젤로 맥스 (Hellvape Jello Max)",
            "유웰 발라리안 맥스 (Uwell Valyrian Max)",
            "아스파이어 아스몬 (Aspire Asmon)",
            "부푸 브이메이트 맥스 (Voopoo Vmate Max)",
            "베이포레소 크로스 4 (Vaporesso XROS 4)"
        ]
        content = "## <a:267042fire:1553325691582292049> VAPE SP0T 인기 기기 TOP 5\n\n"
        emojis = [
            "<a:24171stplace:1553325685827833926>", 
            "<:63082nd:1553325689049055272>", 
            "<:48023rd:1553325687337783346>", 
            "4️⃣", 
            "5️⃣"
        ]
        
        for i, item in enumerate(top_devices):
            content += f"{emojis[i]} **{item}**\n\n"
            
        content += f"-# 🔄 갱신 일시: {date_str} (국내 실시간 판매 데이터 분석 기준)"
        
        payload = create_v2_payload(content.strip())
        url = f"https://discord.com/api/v10/channels/{settings['pop_device_channel_id']}/messages/{settings['pop_device_message_id']}"
        try:
            async with aiohttp.ClientSession() as session:
                await session.patch(url, json=payload, headers=headers)
        except Exception: 
            pass
            
    # 액상 패널 업데이트 (비비빈스 & 일렉샵 인기 트렌드 기준)
    if settings['pop_liquid_channel_id'] and settings['pop_liquid_message_id']:
        top_liquids = [
            "모코 하와이 (Moko Hawaii)",
            "디톡스 알로에베라 (Detox Aloe Vera)",
            "알케마스터 자몽 (Alkemaster Grapefruit)",
            "마르키사 오리지널 (Marquisa Original)",
            "펠릭스 라임라임 (Felix Rhyme Lime)"
        ]
        content = "## <a:267042fire:1541445181176287342> VAPE SP0T 인기 액상 TOP 5\n\n"
        emojis = [
            "<a:24171stplace:1541444890032873532>", 
            "<:63082nd:1541445060242186363>", 
            "<:48023rd:1541445059134627840>", 
            "4️⃣", 
            "5️⃣"
        ]
        
        for i, item in enumerate(top_liquids):
            content += f"{emojis[i]} **{item}**\n\n"
            
        content += f"-# 🔄 갱신 일시: {date_str} (국내 실시간 판매 데이터 분석 기준)"
        
        payload = create_v2_payload(content.strip())
        url = f"https://discord.com/api/v10/channels/{settings['pop_liquid_channel_id']}/messages/{settings['pop_liquid_message_id']}"
        try:
            async with aiohttp.ClientSession() as session:
                await session.patch(url, json=payload, headers=headers)
        except Exception: 
            pass

@bot.tree.command(name="인기기기", description="인기 기기 TOP 5 패널을 생성합니다. (매일 자동 갱신)")
async def popular_devices(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ 관리자만 패널을 생성할 수 있습니다.", ephemeral=True)
        return
        
    if not bot.db_pool: 
        await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=True)
    
    now_kst = datetime.datetime.now(KST)
    update_time = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    date_str = update_time.strftime("%Y-%m-%d %H:%M:%S")
    
    top_devices = [
        "헬베이프 젤로 맥스 (Hellvape Jello Max)",
        "유웰 발라리안 맥스 (Uwell Valyrian Max)",
        "아스파이어 아스몬 (Aspire Asmon)",
        "부푸 브이메이트 맥스 (Voopoo Vmate Max)",
        "베이포레소 크로스 4 (Vaporesso XROS 4)"
    ]
    
    content = "## <a:267042fire:1553325691582292049> VAPE SP0T 인기 기기 TOP 5\n\n"
    emojis = [
        "<a:24171stplace:1553325685827833926>", 
        "<:63082nd:1553325689049055272>", 
        "<:48023rd:1553325687337783346>", 
        "4️⃣", 
        "5️⃣"
    ]
    
    for i, item in enumerate(top_devices):
        content += f"{emojis[i]} **{item}**\n\n"
        
    content += f"-# 🔄 갱신 일시: {date_str} (국내 실시간 판매 데이터 분석 기준)"
    
    # 누구나 볼 수 있는 전체 공개 메시지로 생성 (ephemeral=False)
    payload = create_v2_payload(content.strip(), ephemeral=False)
    
    try:
        url = f"https://discord.com/api/v10/channels/{interaction.channel_id}/messages"
        headers = {
            "Authorization": f"Bot {os.environ.get('BOT_TOKEN')}", 
            "Content-Type": "application/json"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status in (200, 201):
                    msg_id = (await resp.json())['id']
                    
                    async with bot.db_pool.acquire() as conn:
                        await conn.execute('''
                            INSERT INTO guild_settings (guild_id, pop_device_channel_id, pop_device_message_id) 
                            VALUES ($1, $2, $3) 
                            ON CONFLICT (guild_id) DO UPDATE 
                            SET pop_device_channel_id = $2, pop_device_message_id = $3;
                        ''', interaction.guild.id, interaction.channel_id, int(msg_id))
                        
                    # 최종 안내 문구 전송
                    await interaction.followup.send("✅ 인기 기기 패널 생성이 완료되었습니다!", ephemeral=True)
                else:
                    await interaction.followup.send("❌ 패널 생성에 실패했습니다.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"오류가 발생했습니다: {e}", ephemeral=True)

@bot.tree.command(name="인기액상", description="인기 액상 TOP 5 패널을 생성합니다. (매일 자동 갱신)")
async def popular_liquids(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ 관리자만 패널을 생성할 수 있습니다.", ephemeral=True)
        return
        
    if not bot.db_pool: 
        await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=True)
    
    now_kst = datetime.datetime.now(KST)
    update_time = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    date_str = update_time.strftime("%Y-%m-%d %H:%M:%S")
    
    top_liquids = [
        "모코 하와이 (Moko Hawaii)",
        "디톡스 알로에베라 (Detox Aloe Vera)",
        "알케마스터 자몽 (Alkemaster Grapefruit)",
        "마르키사 오리지널 (Marquisa Original)",
        "펠릭스 라임라임 (Felix Rhyme Lime)"
    ]
    
    content = "## <a:267042fire:1541445181176287342> VAPE SP0T 인기 액상 TOP 5\n\n"
    emojis = [
        "<a:24171stplace:1541444890032873532>", 
        "<:63082nd:1541445060242186363>", 
        "<:48023rd:1541445059134627840>", 
        "4️⃣", 
        "5️⃣"
    ]
    
    for i, item in enumerate(top_liquids):
        content += f"{emojis[i]} **{item}**\n\n"
        
    content += f"-# 🔄 갱신 일시: {date_str} (국내 실시간 판매 데이터 분석 기준)"
    
    # 누구나 볼 수 있는 전체 공개 메시지로 생성
    payload = create_v2_payload(content.strip(), ephemeral=False)
    
    try:
        url = f"https://discord.com/api/v10/channels/{interaction.channel_id}/messages"
        headers = {
            "Authorization": f"Bot {os.environ.get('BOT_TOKEN')}", 
            "Content-Type": "application/json"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status in (200, 201):
                    msg_id = (await resp.json())['id']
                    
                    async with bot.db_pool.acquire() as conn:
                        await conn.execute('''
                            INSERT INTO guild_settings (guild_id, pop_liquid_channel_id, pop_liquid_message_id) 
                            VALUES ($1, $2, $3) 
                            ON CONFLICT (guild_id) DO UPDATE 
                            SET pop_liquid_channel_id = $2, pop_liquid_message_id = $3;
                        ''', interaction.guild.id, interaction.channel_id, int(msg_id))
                        
                    await interaction.followup.send("✅ 인기 액상 패널 생성이 완료되었습니다!", ephemeral=True)
                else:
                    await interaction.followup.send("❌ 패널 생성에 실패했습니다.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"오류가 발생했습니다: {e}", ephemeral=True)


# ==========================================
# [기능 10] 실시간 TOP 5 구매 랭킹 시스템
# ==========================================
async def sync_leaderboard_first_role(
    guild_id: int,
    role_id: int | None,
    previous_user_id: int | None,
    first_user_id: int | None,
):
    """구매 랭킹 1위 역할을 현재 1위 한 명에게만 유지합니다."""
    if not bot.db_pool or not role_id:
        return False

    guild = bot.get_guild(guild_id)
    role = guild.get_role(role_id) if guild else None
    if not guild or not role:
        return False

    try:
        if previous_user_id and previous_user_id != first_user_id:
            previous_member = guild.get_member(previous_user_id)
            if previous_member and role in previous_member.roles:
                await previous_member.remove_roles(role, reason="구매 랭킹 1위 변경")

        if first_user_id:
            first_member = guild.get_member(first_user_id)
            if not first_member:
                return False
            if role not in first_member.roles:
                await first_member.add_roles(role, reason="구매 랭킹 1위 달성")
    except (discord.Forbidden, discord.HTTPException):
        logging.exception("구매 랭킹 1위 역할 갱신 실패: guild=%s", guild_id)
        return False

    async with bot.db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE guild_settings SET leaderboard_first_user_id = $2 WHERE guild_id = $1",
            guild_id,
            first_user_id,
        )
    return True


@bot.tree.command(name="구매랭킹역할", description="구매 랭킹 1위에게 자동 지급할 역할을 설정합니다. (관리자 전용)")
@app_commands.describe(역할="현재 구매 랭킹 1위에게 지급할 역할을 선택해 주세요.")
async def set_leaderboard_first_role(interaction: discord.Interaction, 역할: discord.Role):
    if not bot.db_pool:
        await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
        return

    if 역할.is_default() or 역할.managed:
        await interaction.response.send_message("❌ 기본 역할이나 연동 역할은 자동 지급 역할로 설정할 수 없습니다.", ephemeral=True)
        return

    bot_member = interaction.guild.me
    if bot_member and 역할 >= bot_member.top_role:
        await interaction.response.send_message(
            "❌ 봇의 가장 높은 역할보다 낮은 역할만 설정할 수 있습니다.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    async with bot.db_pool.acquire() as conn:
        settings = await conn.fetchrow(
            """
            SELECT leaderboard_first_role_id, leaderboard_first_user_id
            FROM guild_settings WHERE guild_id = $1
            """,
            interaction.guild.id,
        )
        old_role_id = settings.get('leaderboard_first_role_id') if settings else None
        old_user_id = settings.get('leaderboard_first_user_id') if settings else None

        if old_role_id and old_user_id:
            old_role = interaction.guild.get_role(old_role_id)
            old_member = interaction.guild.get_member(old_user_id)
            if old_role and old_member and old_role in old_member.roles:
                try:
                    await old_member.remove_roles(old_role, reason="구매 랭킹 1위 역할 설정 변경")
                except (discord.Forbidden, discord.HTTPException):
                    await interaction.followup.send(
                        "❌ 기존 1위 역할을 회수하지 못했습니다. 봇 역할 순서와 권한을 확인해 주세요.",
                        ephemeral=True,
                    )
                    return

        await conn.execute(
            """
            INSERT INTO guild_settings (guild_id, leaderboard_first_role_id, leaderboard_first_user_id)
            VALUES ($1, $2, NULL)
            ON CONFLICT (guild_id) DO UPDATE
            SET leaderboard_first_role_id = $2, leaderboard_first_user_id = NULL
            """,
            interaction.guild.id,
            역할.id,
        )

    await update_leaderboard(interaction.guild.id)
    await interaction.followup.send(
        f"✅ 구매 랭킹 1위 역할을 {역할.mention}(으)로 설정했습니다. 랭킹이 바뀌면 자동으로 역할을 회수하고 새 1위에게 지급합니다.",
        ephemeral=True,
    )


@bot.tree.command(name="랭킹초기화", description="모든 유저의 누적 구매 금액을 0으로 초기화하여 랭킹을 리셋합니다. (관리자 전용)")
async def reset_leaderboard(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ 관리자만 랭킹을 초기화할 수 있습니다.", ephemeral=True)
        return
        
    if not bot.db_pool: 
        await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=True)
    
    async with bot.db_pool.acquire() as conn:
        # 모든 유저의 누적 구매 금액을 0으로 초기화
        await conn.execute('UPDATE user_info SET total_spent = 0')
        
    # 랭킹 패널 즉시 업데이트 (0원으로 초기화된 상태 반영)
    await update_leaderboard(interaction.guild.id)
    
    await interaction.followup.send("✅ 모든 유저의 누적 구매 금액이 0원으로 초기화되었으며, 랭킹이 리셋되었습니다!", ephemeral=True)


async def update_leaderboard(guild_id: int):
    if not bot.db_pool: 
        return
        
    async with bot.db_pool.acquire() as conn:
        settings = await conn.fetchrow(
            """
            SELECT leaderboard_channel_id, leaderboard_message_id,
                   leaderboard_first_role_id, leaderboard_first_user_id
            FROM guild_settings WHERE guild_id = $1
            """,
            guild_id,
        )

        if not settings:
            return

        top_users = await conn.fetch(
            'SELECT user_id, total_spent FROM user_info '
            'WHERE total_spent > 0 ORDER BY total_spent DESC, user_id ASC LIMIT 5'
        )
        tiers = await conn.fetch('SELECT role_id, required_amount FROM vip_tiers WHERE guild_id = $1 ORDER BY required_amount DESC', guild_id)

    first_user_id = top_users[0]['user_id'] if top_users else None
    if settings.get('leaderboard_first_role_id'):
        await sync_leaderboard_first_role(
            guild_id,
            settings['leaderboard_first_role_id'],
            settings.get('leaderboard_first_user_id'),
            first_user_id,
        )

    if not settings['leaderboard_channel_id'] or not settings['leaderboard_message_id']:
        return

    content = "## <a:267042fire:1553325691582292049> VAPE SP0T 누적 구매 랭킹 TOP 5\n\n"
    
    if not top_users:
        content += "아직 구매 내역이 존재하지 않습니다."
    else:
        emojis = [
            "<a:24171stplace:1553325685827833926>", 
            "<:63082nd:1553325689049055272>", 
            "<:48023rd:1553325687337783346>", 
            "4️⃣", 
            "5️⃣"
        ]
        
        for idx, u in enumerate(top_users):
            user_tier = "일반 고객"
            for t in tiers:
                if u['total_spent'] >= t['required_amount']:
                    user_tier = f"<@&{t['role_id']}>"
                    break
                    
            content += (
                f"{emojis[idx]} **{idx+1}위** : <@{u['user_id']}>\n"
                f"> **등급:** {user_tier}\n"
                f"> **누적금액:** `{u['total_spent']:,}원`\n\n"
            )
            
    payload = {
        "flags": 1 << 15,
        "components": [
            {
                "type": 17,
                "accent_color": 0x32CD32,
                "components": [
                    {
                        "type": 10,
                        "content": content.strip()
                    }
                ]
            }
        ]
    }
    
    try:
        url = f"https://discord.com/api/v10/channels/{settings['leaderboard_channel_id']}/messages/{settings['leaderboard_message_id']}"
        headers = {
            "Authorization": f"Bot {os.environ.get('BOT_TOKEN')}", 
            "Content-Type": "application/json"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.patch(url, json=payload, headers=headers) as resp: 
                pass
    except Exception as e: 
        print(f"랭킹 업데이트 오류가 발생했습니다: {e}")


register_order_edit_command(bot, update_leaderboard)


@bot.tree.command(name="랭킹패널", description="실시간 누적 구매 금액 순위 패널을 생성합니다.")
async def send_leaderboard_panel(interaction: discord.Interaction):
    if not bot.db_pool: 
        await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=True)
    
    content = "## <a:267042fire:1553325691582292049> VAPE SP0T 누적 구매 랭킹 TOP 5\n\n랭킹 데이터를 불러오는 중입니다..."
    
    payload = {
        "flags": 1 << 15,
        "components": [
            {
                "type": 17,
                "accent_color": 0x32CD32,
                "components": [
                    {
                        "type": 10,
                        "content": content
                    }
                ]
            }
        ]
    }
    
    try:
        url = f"https://discord.com/api/v10/channels/{interaction.channel_id}/messages"
        headers = {
            "Authorization": f"Bot {os.environ.get('BOT_TOKEN')}", 
            "Content-Type": "application/json"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status in (200, 201):
                    msg_id = (await resp.json())['id']
                    
                    async with bot.db_pool.acquire() as conn:
                        await conn.execute('''
                            INSERT INTO guild_settings (guild_id, leaderboard_channel_id, leaderboard_message_id) 
                            VALUES ($1, $2, $3) 
                            ON CONFLICT (guild_id) DO UPDATE 
                            SET leaderboard_channel_id = $2, leaderboard_message_id = $3;
                        ''', interaction.guild.id, interaction.channel_id, int(msg_id))
                        
                    await interaction.followup.send("✅ 랭킹 패널 생성이 완료되었습니다!", ephemeral=True)
                    await update_leaderboard(interaction.guild.id)
                else:
                    await interaction.followup.send("❌ 랭킹 패널 생성에 실패했습니다.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"오류가 발생했습니다: {e}", ephemeral=True)


@tasks.loop(minutes=10)
async def leaderboard_updater():
    if not bot.db_pool: 
        return
        
    async with bot.db_pool.acquire() as conn:
        settings_list = await conn.fetch('SELECT guild_id FROM guild_settings WHERE leaderboard_channel_id IS NOT NULL')
        
        for g in settings_list:
            await update_leaderboard(g['guild_id'])
            # 10분마다 인기품목 패널들도 함께 업데이트 되도록 처리
            await update_popular_panels(g['guild_id'])


@leaderboard_updater.before_loop
async def before_updater(): 
    await bot.wait_until_ready()


# ==========================================
# [기능 6] VIP 등급 설정 시스템
# ==========================================
class TierSetupGroup(app_commands.Group):
    def __init__(self): 
        super().__init__(name="등급설정", description="VIP 등급을 설정하고 관리합니다.")

    @app_commands.command(name="제작", description="새로운 등급을 추가하거나 수정합니다.")
    async def create_tier(self, interaction: discord.Interaction, 역할: discord.Role, 누적구매액수: int):
        if not bot.db_pool: 
            await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
            return
            
        async with bot.db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO vip_tiers (guild_id, role_id, required_amount) 
                VALUES ($1, $2, $3) 
                ON CONFLICT (guild_id, role_id) DO UPDATE SET required_amount = $3;
            ''', interaction.guild.id, 역할.id, 누적구매액수)
            
        await interaction.response.send_message(f"✅ {역할.mention} 등급이 `{누적구매액수:,}원` 조건으로 설정 완료되었습니다.", ephemeral=True)

    @app_commands.command(name="삭제", description="기존 등급을 삭제합니다.")
    async def delete_tier(self, interaction: discord.Interaction, 역할: discord.Role):
        if not bot.db_pool: 
            await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
            return
            
        async with bot.db_pool.acquire() as conn:
            res = await conn.execute('DELETE FROM vip_tiers WHERE guild_id = $1 AND role_id = $2', interaction.guild.id, 역할.id)
            
        if res == "DELETE 0": 
            await interaction.response.send_message("❌ 등록된 등급이 없습니다.", ephemeral=True)
        else: 
            await interaction.response.send_message(f"✅ {역할.mention} 삭제가 완료되었습니다.", ephemeral=True)

    @app_commands.command(name="목록", description="설정된 등급 목록 확인.")
    async def list_tier(self, interaction: discord.Interaction):
        if not bot.db_pool: 
            await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
            return
            
        async with bot.db_pool.acquire() as conn:
            tiers = await conn.fetch('SELECT role_id, required_amount FROM vip_tiers WHERE guild_id = $1 ORDER BY required_amount DESC', interaction.guild.id)
            
        if not tiers: 
            await interaction.response.send_message("❌ 설정된 등급이 없습니다.", ephemeral=True)
            return
        
        content = "## 🏆 VIP 등급 설정 목록\n\n"
        for idx, t in enumerate(tiers, 1): 
            content += f"`{idx}.` <@&{t['role_id']}> : `{t['required_amount']:,}원` 이상\n\n"
            
        payload = {
            "flags": (1 << 15) | (1 << 6), 
            "components": [
                {
                    "type": 17, 
                    "accent_color": 0x32CD32, 
                    "components": [
                        {
                            "type": 10, 
                            "content": content.strip()
                        }
                    ]
                }
            ]
        }
        
        await interaction.client.http.request(
            discord.http.Route("POST", f"/interactions/{interaction.id}/{interaction.token}/callback"), 
            json={"type": 4, "data": payload}
        )

bot.tree.add_command(TierSetupGroup())


# ==========================================
# [기능 7] 커스텀 컨테이너 & 임베드 생성 모달 
# ==========================================
class CustomContainerModal(discord.ui.Modal, title="📦 컨테이너 메시지 폼"):
    title_input = discord.ui.TextInput(
        label="제목", 
        style=discord.TextStyle.short, 
        required=False
    )
    content_input = discord.ui.TextInput(
        label="내용 (=== 는 선, \\n은 줄바꿈)", 
        style=discord.TextStyle.paragraph, 
        required=False
    )
    image_input = discord.ui.TextInput(
        label="이미지 URL", 
        style=discord.TextStyle.short, 
        required=False
    )
    button_labels_input = discord.ui.TextInput(
        label="버튼 이름 (, 로 구분)", 
        style=discord.TextStyle.short, 
        required=False
    )
    button_links_input = discord.ui.TextInput(
        label="버튼 링크 (, 로 구분)", 
        style=discord.TextStyle.short, 
        required=False
    )

    async def on_submit(self, interaction: discord.Interaction):
        children = []
        
        if self.title_input.value.strip(): 
            children.append({
                "type": 10, 
                "content": self.title_input.value.strip()
            })
            
        if self.content_input.value.strip():
            real_content = self.content_input.value.strip().replace('\\n', '\n')
            parts = real_content.split("===")
            
            for i, p in enumerate(parts):
                if p.strip(): 
                    children.append({
                        "type": 10, 
                        "content": p.strip()
                    })
                if i < len(parts) - 1: 
                    children.append({
                        "type": 14, 
                        "divider": True
                    })
                    
        if self.image_input.value.strip(): 
            children.append({
                "type": 12, 
                "items": [
                    {
                        "media": {
                            "url": self.image_input.value.strip()
                        }
                    }
                ]
            })
        
        btns = []
        lbls = [l.strip() for l in self.button_labels_input.value.split(",") if l.strip()]
        lnks = [l.strip() for l in self.button_links_input.value.split(",") if l.strip()]
        
        for i in range(min(len(lbls), 3)):
            link = lnks[i] if i < len(lnks) else None
            
            if link and link.startswith("http"): 
                btns.append({
                    "type": 2, 
                    "style": 5, 
                    "label": lbls[i], 
                    "url": link
                })
            else: 
                btns.append({
                    "type": 2, 
                    "style": 1, 
                    "label": lbls[i], 
                    "custom_id": f"cbtn_{i}_{int(time.time()*1000)}"
                })
                
        if btns: 
            children.append({
                "type": 1, 
                "components": btns
            })

        callback_url = f"https://discord.com/api/v10/interactions/{interaction.id}/{interaction.token}/callback"

        if not children:
            error_payload = {
                "type": 4, 
                "data": {
                    "content": "⚠️ 내용이 없습니다.", 
                    "flags": 64
                }
            }
            try: 
                async with interaction.client.http._HTTPClient__session.post(callback_url, json=error_payload) as resp: 
                    pass
            except Exception: 
                pass
            return

        success_payload = {
            "type": 4, 
            "data": {
                "content": "✔ 성공적으로 생성되었습니다.", 
                "flags": 64
            }
        }
        
        try: 
            async with interaction.client.http._HTTPClient__session.post(callback_url, json=success_payload) as resp: 
                pass
        except Exception: 
            pass

        msg_payload = {
            "flags": 1 << 15, 
            "components": [
                {
                    "type": 17, 
                    "accent_color": 0x32CD32, 
                    "components": children
                }
            ]
        }
        
        try:
            channel_url = f"https://discord.com/api/v10/channels/{interaction.channel_id}/messages"
            headers = {
                "Authorization": f"Bot {os.environ.get('BOT_TOKEN')}", 
                "Content-Type": "application/json"
            }
            async with interaction.client.http._HTTPClient__session.post(channel_url, json=msg_payload, headers=headers) as resp:
                if resp.status not in (200, 201, 204):
                    webhook_url = f"https://discord.com/api/v10/webhooks/{bot.user.id}/{interaction.token}"
                    async with interaction.client.http._HTTPClient__session.post(webhook_url, json=msg_payload) as web_resp: 
                        pass
        except Exception: 
            pass

@bot.tree.command(name="컨테이너", description="입력창(Form)을 열어 컨테이너 메시지를 생성합니다!")
async def container_command(interaction: discord.Interaction):
    await interaction.response.send_modal(CustomContainerModal())


# ---------------------------------------------------------
# [기능 7.1] 기본 임베드(Embed) 생성 팝업(모달)
# ---------------------------------------------------------
class CustomEmbedModal(discord.ui.Modal, title="📝 임베드 메시지 생성 폼"):
    embed_title = discord.ui.TextInput(
        label="제목 (선택)", 
        style=discord.TextStyle.short, 
        placeholder="임베드 제목을 입력해 주세요.", 
        required=False, 
        max_length=256
    )
    embed_desc = discord.ui.TextInput(
        label="내용 (필수)", 
        style=discord.TextStyle.paragraph, 
        placeholder="임베드 내용을 입력해 주세요. (\\n 으로 줄바꿈 가능)", 
        required=True, 
        max_length=4000
    )
    embed_color = discord.ui.TextInput(
        label="색상 HEX 코드 (선택, 예: FF0000)", 
        style=discord.TextStyle.short, 
        placeholder="입력하지 않으면 기본 녹색(32CD32)이 적용됩니다.", 
        required=False, 
        max_length=6
    )
    embed_image = discord.ui.TextInput(
        label="이미지 URL (선택)", 
        style=discord.TextStyle.short, 
        placeholder="예: https://example.com/image.png", 
        required=False
    )
    embed_footer = discord.ui.TextInput(
        label="푸터 텍스트 (선택)", 
        style=discord.TextStyle.short, 
        placeholder="임베드 맨 아래에 들어갈 문구를 입력해 주세요.", 
        required=False, 
        max_length=2048
    )

    async def on_submit(self, interaction: discord.Interaction):
        color_val = 0x32CD32
        
        if self.embed_color.value.strip():
            try:
                color_val = int(self.embed_color.value.strip().replace("#", ""), 16)
            except ValueError:
                pass
                
        real_desc = self.embed_desc.value.replace('\\n', '\n')
                
        embed = discord.Embed(
            description=real_desc,
            color=color_val
        )
        
        if self.embed_title.value.strip():
            embed.title = self.embed_title.value.strip()
            
        if self.embed_image.value.strip():
            if self.embed_image.value.startswith("http"):
                embed.set_image(url=self.embed_image.value.strip())
                
        if self.embed_footer.value.strip():
            embed.set_footer(text=self.embed_footer.value.strip())

        await interaction.response.send_message("✅ 임베드 메시지가 성공적으로 전송되었습니다!", ephemeral=True)
        await interaction.channel.send(embed=embed)

@bot.tree.command(name="임베드", description="입력창(Form)을 열어 임베드 메시지를 생성합니다!")
async def embed_command(interaction: discord.Interaction):
    await interaction.response.send_modal(CustomEmbedModal())


# ==========================================
# [기능 8] 후기 시스템 (파라미터 명령어 방식)
# ==========================================
@bot.tree.command(name="후기작성", description="구매 후기와 사진을 작성합니다. (구매자 전용)")
@app_commands.describe(
    별점="별점을 선택해 주세요.", 
    후기내용="최소 10자 이상 솔직한 후기를 남겨주세요. (미리 복사해 붙여넣거나 \\n 입력 시 줄바꿈 지원)", 
    사진="후기 사진을 함께 첨부해 주세요. (선택)"
)
@app_commands.choices(별점=[
    app_commands.Choice(name="⭐⭐⭐⭐⭐ (5점)", value=5), 
    app_commands.Choice(name="⭐⭐⭐⭐ (4점)", value=4),
    app_commands.Choice(name="⭐⭐⭐ (3점)", value=3), 
    app_commands.Choice(name="⭐⭐ (2점)", value=2), 
    app_commands.Choice(name="⭐ (1점)", value=1)
])
async def write_review(
    interaction: discord.Interaction, 
    별점: app_commands.Choice[int], 
    후기내용: str, 
    사진: discord.Attachment = None
):
    if not bot.db_pool: 
        await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
        return
        
    async with bot.db_pool.acquire() as conn:
        settings = await conn.fetchrow(
            '''
            SELECT buyer_role_id, review_channel_id, review_auto_message, anonymous_role_id
            FROM guild_settings WHERE guild_id = $1
            ''',
            interaction.guild.id,
        )
        
    if not settings or not settings['buyer_role_id']: 
        await interaction.response.send_message("❌ 구매자 역할이 설정되지 않았습니다.", ephemeral=True)
        return
        
    buyer_role = interaction.guild.get_role(settings['buyer_role_id'])
    
    if not buyer_role or buyer_role not in interaction.user.roles: 
        await interaction.response.send_message("❌ **구매자 전용** 기능입니다. 결제하신 유저만 작성 가능합니다.", ephemeral=True)
        return
        
    if len(후기내용) < 10: 
        await interaction.response.send_message("❌ 후기는 최소 10글자 이상 작성해 주시기 바랍니다.", ephemeral=True)
        return
        
    if not settings['review_channel_id']: 
        await interaction.response.send_message("❌ 관리자가 후기 채널을 설정하지 않았습니다.", ephemeral=True)
        return
        
    review_channel = interaction.guild.get_channel(settings['review_channel_id'])
    
    if not review_channel: 
        await interaction.response.send_message("❌ 지정된 후기 채널을 찾을 수 없습니다.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    
    stars_str = "⭐" * 별점.value
    anonymous_role_id = settings.get('anonymous_role_id')
    review_buyer = f"<@&{anonymous_role_id}>" if anonymous_role_id else "익명"
    
    content_1 = (
        "## 구매후기\n"
        "**\n"
        f"- 구매자 : {review_buyer}\n"
        "**"
    )
    
    real_review_content = 후기내용.replace('\\n', '\n')
    
    container_children = [
        {
            "type": 10, 
            "content": content_1
        }, 
        {
            "type": 14, 
            "divider": True
        },
        {
            "type": 10, 
            "content": f"**{stars_str}**"
        }, 
        {
            "type": 14, 
            "divider": True
        },
        {
            "type": 10, 
            "content": f"**{real_review_content}**"
        }
    ]

    watermarked_file = None
    if 사진:
        if not 사진.content_type or not 사진.content_type.startswith('image/'): 
            await interaction.followup.send("❌ 이미지 파일만 업로드 가능합니다.", ephemeral=True)
            return

        try:
            original_image = await 사진.read()
            watermarked_image = await asyncio.to_thread(watermark_review_image, original_image)
        except ReviewImageError as error:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.followup.send("❌ 후기 이미지를 다운로드하지 못했습니다. 다시 업로드해 주세요.", ephemeral=True)
            return
        except Exception:
            logging.exception("후기 이미지 워터마크 처리 실패: user=%s", interaction.user.id)
            await interaction.followup.send("❌ 후기 이미지 처리 중 오류가 발생했습니다. 다른 이미지로 다시 시도해 주세요.", ephemeral=True)
            return

        watermarked_filename = f"review-{interaction.id}.jpg"
        watermarked_file = discord.File(io.BytesIO(watermarked_image), filename=watermarked_filename)
        container_children.append({
            "type": 14, 
            "divider": True
        })
        container_children.append({
            "type": 12, 
            "items": [
                {
                    "media": {
                        "url": f"attachment://{watermarked_filename}"
                    }
                }
            ]
        })

    review_payload = {
        "flags": 1 << 15, 
        "allowed_mentions": (
            {"parse": [], "roles": [str(anonymous_role_id)]}
            if anonymous_role_id else {"parse": []}
        ),
        "components": [
            {
                "type": 17, 
                "accent_color": 0x32CD32, 
                "components": container_children
            }
        ]
    }

    if watermarked_file:
        review_payload["attachments"] = [watermarked_file.to_dict(0)]

    # 1. 후기 등록
    route = discord.http.Route("POST", f"/channels/{review_channel.id}/messages")
    if watermarked_file:
        with discord.http.handle_message_parameters(file=watermarked_file) as params:
            params.multipart[0]["value"] = json.dumps(
                review_payload, ensure_ascii=False, separators=(",", ":")
            )
            await interaction.client.http.request(route, files=params.files, form=params.multipart)
    else:
        await interaction.client.http.request(route, json=review_payload)
    
    reward_text = ""
    try:
        async with bot.db_pool.acquire() as conn:
            reward, balance = await award_review_points(conn, interaction.guild.id, interaction.user.id, interaction.id)
        reward_text = f"\n🎁 {reward:,}P 적립 / 현재 보유 포인트: {balance:,}P"
    except Exception as exc:
        print(f"후기 포인트 적립 실패 (interaction_id={interaction.id}): {type(exc).__name__}")
        reward_text = "\n⚠️ 후기는 등록되었으나 포인트 적립에 실패했습니다. 관리자에게 문의해 주세요."

    # 2. 기존 후기 안내를 지운 뒤 새 안내를 후기 목록 맨 아래로 이동
    if settings.get('review_auto_message'):
        try:
            async with bot.db_pool.acquire() as conn:
                await replace_review_guide_message(
                    conn,
                    interaction.client.http,
                    interaction.guild.id,
                    review_channel.id,
                    settings['review_auto_message'],
                )
        except Exception:
            logging.exception("후기 자동 안내 교체 실패: guild=%s", interaction.guild.id)
    
    await interaction.followup.send("✅ 후기 등록이 정상적으로 완료되었습니다!" + reward_text, ephemeral=True)


# ==========================================
# [기능 11] 포럼 포스트 자동 백업 & 생성 기능
# ==========================================
@bot.tree.command(name="포스트생성", description="지정한 포럼 채널에 새로운 포스트를 작성합니다.")
@app_commands.describe(
    채널="포스트를 작성할 포럼 채널을 선택해 주세요.",
    제목="포스트의 제목을 입력해 주세요.",
    내용="포스트의 본문 내용을 입력해 주세요. (미리 작성 후 붙여넣거나 \\n 입력 시 줄바꿈 지원)",
    사진="첨부할 사진 파일이 있다면 업로드해 주세요. (선택)"
)
async def create_forum_post(
    interaction: discord.Interaction,
    채널: discord.ForumChannel,
    제목: str,
    내용: str,
    사진: discord.Attachment = None
):
    await interaction.response.defer(ephemeral=True)

    real_content = 내용.replace('\\n', '\n')

    try:
        kwargs = {
            "name": 제목,
            "content": real_content
        }
        
        if 사진:
            file = await 사진.to_file()
            kwargs["file"] = file
            
        thread_with_message = await 채널.create_thread(**kwargs)
        
        await interaction.followup.send(f"✅ 포스트 생성이 완료되었습니다! 바로가기: {thread_with_message.thread.mention}", ephemeral=True)
        
    except discord.Forbidden:
        await interaction.followup.send("❌ 봇에게 해당 포럼 채널에 포스트를 생성할 권한이 없습니다.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 포스트 생성 중 오류가 발생했습니다: {e}", ephemeral=True)


@bot.tree.command(name="기기가격표", description="포럼 채널에 가격표 포스트를 작성하고 DB에 영구 백업합니다.")
@app_commands.describe(
    채널="포스트를 작성할 포럼 채널을 선택해 주세요.",
    기기명="포스트의 제목(기기명)을 입력해 주세요.",
    사진="기기 사진을 업로드해 주세요.",
    가격="기기의 가격을 입력해 주세요. (숫자만 또는 단위 포함)",
    색상="기기의 색상들을 입력해 주세요. (미리 작성 후 붙여넣거나 \\n 입력 시 줄바꿈 지원)"
)
async def create_device_price_post(
    interaction: discord.Interaction,
    채널: discord.ForumChannel,
    기기명: str,
    사진: discord.Attachment,
    가격: str,
    색상: str
):
    await interaction.response.defer(ephemeral=True)
    
    real_colors = 색상.replace('\\n', '\n')
    
    content = (
        f"- 가격 : {가격} ( 택배비, 수수료 포함 )\n"
        f"- 색상 : {real_colors}\n"
        "- 구매문의 : <#1553664487117168640>\n"
        "** 품절 현황은 티켓에서! **\n"
        "-# <a:__:1553621821268697128> 재고 및 가격은 GS와 CU가 다를 수 있습니다."
    )
    
    # DB 자동 백업 시스템
    if bot.db_pool:
        async with bot.db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO price_lists (guild_id, type, name, price, options, image_url) 
                VALUES ($1, $2, $3, $4, $5, $6) 
                ON CONFLICT (guild_id, name) DO UPDATE 
                SET type = $2, price = $4, options = $5, image_url = $6;
            ''', interaction.guild.id, '기기', 기기명, 가격, 색상, 사진.url if 사진 else "")
    
    try:
        file = await 사진.to_file()
        thread_with_message = await 채널.create_thread(
            name=기기명,
            content=content,
            file=file
        )
        await interaction.followup.send(f"✅ 기기 가격표가 생성되고 시스템에 저장되었습니다! 바로가기: {thread_with_message.thread.mention}", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ 봇에게 해당 포럼 채널에 포스트를 생성할 권한이 없습니다.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 포스트 생성 중 오류가 발생했습니다: {e}", ephemeral=True)


@bot.tree.command(name="일회용가격표", description="포럼 채널에 일회용 가격표 포스트를 작성하고 DB에 영구 백업합니다.")
@app_commands.describe(
    채널="포스트를 작성할 포럼 채널을 선택해 주세요.",
    일회용명="포스트의 제목(일회용명)을 입력해 주세요.",
    사진="일회용 제품 사진을 업로드해 주세요.",
    가격="일회용 제품의 가격을 입력해 주세요. (숫자만 또는 단위 포함)",
    맛="일회용 제품의 맛을 입력해 주세요. (미리 작성 후 붙여넣거나 \\n 입력 시 줄바꿈 지원)"
)
async def create_disposable_price_post(
    interaction: discord.Interaction,
    채널: discord.ForumChannel,
    일회용명: str,
    사진: discord.Attachment,
    가격: str,
    맛: str
):
    await interaction.response.defer(ephemeral=True)

    real_flavors = 맛.replace('\\n', '\n')
    content = (
        f"- 가격 : {가격} ( 택배비, 수수료 포함 )\n"
        f"- 맛 : {real_flavors}\n"
        "- 구매문의 : <#1553664487117168640>\n"
        "** 품절 현황은 티켓에서! **\n"
        "-# <a:__:1553621821268697128> 재고 및 가격은 GS와 CU가 다를 수 있습니다."
    )

    if bot.db_pool:
        async with bot.db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO price_lists (guild_id, type, name, price, options, image_url)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (guild_id, name) DO UPDATE
                SET type = $2, price = $4, options = $5, image_url = $6;
            ''', interaction.guild.id, '일회용', 일회용명, 가격, 맛, 사진.url if 사진 else "")

    try:
        file = await 사진.to_file()
        thread_with_message = await 채널.create_thread(
            name=일회용명,
            content=content,
            file=file
        )
        await interaction.followup.send(
            f"✅ 일회용 가격표가 생성되고 시스템에 저장되었습니다! 바로가기: {thread_with_message.thread.mention}",
            ephemeral=True,
        )
    except discord.Forbidden:
        await interaction.followup.send("❌ 봇에게 해당 포럼 채널에 포스트를 생성할 권한이 없습니다.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 포스트 생성 중 오류가 발생했습니다: {e}", ephemeral=True)


@bot.tree.command(name="액상가격표", description="포럼 채널에 가격표 포스트를 작성하고 DB에 영구 백업합니다.")
@app_commands.describe(
    채널="포스트를 작성할 포럼 채널을 선택해 주세요.",
    액상명="포스트의 제목(액상명)을 입력해 주세요.",
    사진="액상 사진을 업로드해 주세요.",
    가격="액상의 가격을 입력해 주세요. (숫자만 또는 단위 포함)",
    맛="액상의 맛을 입력해 주세요. (미리 작성 후 붙여넣거나 \\n 입력 시 줄바꿈 지원)"
)
async def create_liquid_price_post(
    interaction: discord.Interaction,
    채널: discord.ForumChannel,
    액상명: str,
    사진: discord.Attachment,
    가격: str,
    맛: str
):
    await interaction.response.defer(ephemeral=True)
    
    real_flavors = 맛.replace('\\n', '\n')
    
    content = (
        f"- 가격 : {가격} ( 택배비, 수수료 포함 )\n"
        f"- 맛 : {real_flavors}\n"
        "- 구매문의 : <#1553664487117168640>\n"
        "** 품절 현황은 티켓에서! **\n"
        "-# <a:__:1553621821268697128> 재고 및 가격은 GS와 CU가 다를 수 있습니다."
    )
    
    # DB 자동 백업 시스템
    if bot.db_pool:
        async with bot.db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO price_lists (guild_id, type, name, price, options, image_url) 
                VALUES ($1, $2, $3, $4, $5, $6) 
                ON CONFLICT (guild_id, name) DO UPDATE 
                SET type = $2, price = $4, options = $5, image_url = $6;
            ''', interaction.guild.id, '액상', 액상명, 가격, 맛, 사진.url if 사진 else "")
    
    try:
        file = await 사진.to_file()
        thread_with_message = await 채널.create_thread(
            name=액상명,
            content=content,
            file=file
        )
        await interaction.followup.send(f"✅ 액상 가격표가 생성되고 시스템에 저장되었습니다! 바로가기: {thread_with_message.thread.mention}", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ 봇에게 해당 포럼 채널에 포스트를 생성할 권한이 없습니다.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 포스트 생성 중 오류가 발생했습니다: {e}", ephemeral=True)


@bot.tree.command(name="가격표복구", description="DB에 저장된 모든 가격표(기기/액상/일회용)를 지정한 포럼 채널에 복구합니다.")
@app_commands.describe(채널="가격표를 한 번에 복구할 포럼 채널을 선택해 주세요.")
async def restore_price_lists(interaction: discord.Interaction, 채널: discord.ForumChannel):
    if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
        
    if not bot.db_pool:
        await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=True)
    
    async with bot.db_pool.acquire() as conn:
        rows = await conn.fetch('SELECT * FROM price_lists WHERE guild_id = $1', interaction.guild.id)
        
    if not rows:
        await interaction.followup.send("❌ DB에 저장된 가격표가 없습니다. `/기기가격표`, `/액상가격표`, `/일회용가격표`로 먼저 등록해 주세요.", ephemeral=True)
        return
        
    success_cnt = 0
    fail_cnt = 0
    
    for row in rows:
        p_type = row['type']
        name = row['name']
        price = row['price']
        options = row['options']
        img_url = row['image_url']
        
        real_options = options.replace('\\n', '\n')
        
        if p_type == '기기':
            content = (
                f"- 가격 : {price} ( 택배비, 수수료 포함 )\n"
                f"- 색상 : {real_options}\n"
                "- 구매문의 : <#1553664487117168640>\n"
                "** 품절 현황은 티켓에서! **\n"
                "-# <a:__:1553621821268697128> 재고 및 가격은 GS와 CU가 다를 수 있습니다."
            )
        elif p_type in ('액상', '일회용'):
            content = (
                f"- 가격 : {price} ( 택배비, 수수료 포함 )\n"
                f"- 맛 : {real_options}\n"
                "- 구매문의 : <#1553664487117168640>\n"
                "** 품절 현황은 티켓에서! **\n"
                "-# <a:__:1553621821268697128> 재고 및 가격은 GS와 CU가 다를 수 있습니다."
            )
        else:
            print(f"알 수 없는 가격표 종류를 건너뜁니다: {p_type}")
            fail_cnt += 1
            continue
            
        file = None
        try:
            if img_url:
                async with aiohttp.ClientSession() as session:
                    async with session.get(img_url) as resp:
                        if resp.status == 200:
                            img_data = await resp.read()
                            file = discord.File(io.BytesIO(img_data), filename=f"{name}.png")
                            
            kwargs = {"name": name, "content": content}
            if file:
                kwargs["file"] = file
                
            await 채널.create_thread(**kwargs)
            success_cnt += 1
        except Exception as e:
            print(f"가격표 복구 실패 ({name}): {e}")
            fail_cnt += 1
            
        # 디스코드 API Rate Limit 방지
        await asyncio.sleep(1.5)
        
    await interaction.followup.send(f"✅ 가격표 복구 완료!\n- **성공:** `{success_cnt}건`\n- **실패:** `{fail_cnt}건`\n지정하신 포럼 채널({채널.mention})을 확인해 주세요.", ephemeral=True)


# ==========================================
# [기능 9] 모든 버튼/드롭다운 클릭 이벤트 통제 센터
# ==========================================
@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component: 
        return
        
    custom_id = interaction.data.get("custom_id", "")

    if custom_id.startswith("chat_rank_page:"):
        await handle_rank_page(interaction, bot)
        return

    if custom_id.startswith("join_application_decision:"):
        await handle_application_decision(interaction, bot.db_pool)
        return

    if custom_id.startswith("join_application:"):
        await handle_application_button(interaction, bot.db_pool)
        return

    if custom_id == "point_balance_view":
        await show_my_points(interaction)
        return

    if custom_id == "store_address_brand":
        await handle_brand_selection(interaction)
        return

    if custom_id == "chat_points_rewards" or custom_id.startswith("chat_points_open_box:"):
        await handle_reward_interaction(interaction, bot.db_pool)
        return

    # [출퇴근 버튼 처리]
    if custom_id == "clock_other":
        await interaction.response.send_modal(OtherClockOutModal())
        return

    if custom_id in ["clock_in", "clock_out", "clock_away", "clock_sleep"]:
        await process_clock_action(interaction, custom_id)
        return

    # 배송조회 처리
    if custom_id == "select_courier":
        val = interaction.data["values"][0]
        await interaction.response.send_modal(TrackingModal(val.split("|")[0], val.split("|")[1]))
        return

    # 정보 패널 처리
    if custom_id in ["info_register", "info_edit", "info_view"]:
        if interaction.guild is None or bot.db_pool is None:
            await interaction.response.send_message("❌ 서버와 DB 연결을 확인해 주세요.", ephemeral=True)
            return
        async with bot.db_pool.acquire() as conn:
            user_data = await conn.fetchrow('SELECT * FROM user_info WHERE user_id = $1', interaction.user.id)
            
            if custom_id == "info_register":
                await interaction.response.send_modal(UserInfoModal())
                
            elif custom_id == "info_edit":
                if not user_data: 
                    await interaction.response.send_message("❌ 먼저 등록해 주시기 바랍니다.", ephemeral=True)
                    return
                await interaction.response.send_modal(UserInfoModal(existing_data=user_data))
                
            elif custom_id == "info_view":
                point_balance = await get_balance(conn, interaction.guild.id, interaction.user.id)
                if not user_data:
                    user_data = {"name": "미등록", "contact": "미등록", "address": "미등록",
                                 "cvs": "미등록"}
                
                view_content = (
                    "## 📋 내 정보 조회\n\n"
                    "`💰`**현재 보유 포인트**\n"
                    f"`{point_balance:,}P`\n\n"
                    "`👤`**이름**\n"
                    f"`{user_data['name']}`\n\n"
                    "`📞`**연락처**\n"
                    f"`{user_data['contact']}`\n\n"
                    "`🏠`**주소**\n"
                    f"`{user_data['address']}`\n\n"
                    "`🏪`**편의점**\n"
                    f"`{user_data['cvs']}`"
                )
                
                view_payload = {
                    "flags": (1 << 15) | (1 << 6), 
                    "components": [
                        {
                            "type": 17, 
                            "accent_color": 0x32CD32, 
                            "components": [
                                {
                                    "type": 10, 
                                    "content": view_content
                                }
                            ]
                        }
                    ]
                }
                
                await interaction.client.http.request(
                    discord.http.Route("POST", f"/interactions/{interaction.id}/{interaction.token}/callback"), 
                    json={"type": 4, "data": view_payload}
                )
                
        return

    # 티켓 생성 드롭다운
    if custom_id == "select_ticket_type":
        t_type = interaction.data["values"][0]
        if t_type == "purchase": 
            await interaction.response.send_modal(PurchaseTicketModal())
        elif t_type == "general": 
            await interaction.response.send_modal(GeneralTicketModal())
        elif t_type == "partner": 
            await interaction.response.send_modal(PartnerTicketModal())
        return

    # 티켓 닫기 버튼
    if custom_id == "ticket_close":
        if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("❌ 관리자만 티켓을 닫을 수 있습니다.", ephemeral=True)
            return
            
        await interaction.response.defer()
        
        async with bot.db_pool.acquire() as conn:
            ticket = await conn.fetchrow('SELECT * FROM tickets WHERE channel_id = $1', interaction.channel.id)
            settings = await conn.fetchrow('SELECT * FROM guild_settings WHERE guild_id = $1', interaction.guild.id)
            
            if ticket and settings:
                archive_cat_id = settings[f"archive_cat_{ticket['ticket_type']}"]
                if archive_cat_id:
                    archive_cat = interaction.guild.get_channel(archive_cat_id)
                    if archive_cat: 
                        await interaction.channel.edit(category=archive_cat)
                        
            if ticket:
                member = interaction.guild.get_member(ticket['user_id'])
                if member: 
                    await interaction.channel.set_permissions(member, view_channel=False, read_messages=False, send_messages=False)
        
        close_payload = {
            "flags": 1 << 15, 
            "components": [
                {
                    "type": 17, 
                    "accent_color": 0xFF0000, 
                    "components": [
                        {
                            "type": 10, 
                            "content": "## 🔒 티켓이 닫혔습니다.\n\n이 티켓은 더 이상 대화하실 수 없으며 보관소로 이동되었습니다."
                        }, 
                        {
                            "type": 1, 
                            "components": [
                                {
                                    "type": 2,
                                    "style": 3,
                                    "label": "티켓 재개",
                                    "custom_id": "ticket_reopen",
                                    "emoji": {"name": "🔓"}
                                },
                                {
                                    "type": 2, 
                                    "style": 4, 
                                    "label": "티켓 영구 삭제 및 저장", 
                                    "custom_id": "ticket_delete"
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        
        await interaction.client.http.request(
            discord.http.Route("POST", f"/channels/{interaction.channel.id}/messages"), 
            json=close_payload
        )
        return

    # 닫힌 티켓 재개 버튼
    if custom_id == "ticket_reopen":
        if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("❌ 관리자만 티켓을 재개할 수 있습니다.", ephemeral=True)
            return
        if not bot.db_pool:
            await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        async with bot.db_pool.acquire() as conn:
            ticket = await conn.fetchrow('SELECT * FROM tickets WHERE channel_id = $1', interaction.channel.id)
            settings = await conn.fetchrow('SELECT * FROM guild_settings WHERE guild_id = $1', interaction.guild.id)

        if not ticket:
            await interaction.followup.send("❌ 티켓 정보를 찾을 수 없습니다.", ephemeral=True)
            return
        if not settings:
            await interaction.followup.send("❌ 서버의 티켓 설정을 찾을 수 없습니다.", ephemeral=True)
            return

        active_cat_id = settings.get(f"ticket_cat_{ticket['ticket_type']}")
        active_category = interaction.guild.get_channel(active_cat_id) if active_cat_id else None
        if not active_category:
            await interaction.followup.send("❌ 원래 티켓 카테고리를 찾을 수 없습니다. `/티켓카테고리`를 다시 설정해 주세요.", ephemeral=True)
            return

        member = interaction.guild.get_member(ticket['user_id'])
        if member is None:
            try:
                member = await interaction.guild.fetch_member(ticket['user_id'])
            except discord.HTTPException:
                await interaction.followup.send("❌ 티켓을 연 유저가 서버에 없어 티켓을 재개할 수 없습니다.", ephemeral=True)
                return

        already_open = interaction.channel.category_id == active_cat_id
        try:
            await interaction.channel.edit(category=active_category)
            await interaction.channel.set_permissions(
                member, view_channel=True, read_messages=True, send_messages=True
            )
        except discord.Forbidden:
            await interaction.followup.send("❌ 봇에 채널 관리 권한이 없어 티켓을 재개할 수 없습니다.", ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.followup.send("❌ Discord에서 티켓을 재개하지 못했습니다. 잠시 후 다시 시도해 주세요.", ephemeral=True)
            return

        if already_open:
            await interaction.followup.send("✅ 이미 재개된 티켓입니다. 티켓을 연 유저의 접근 권한을 복구했습니다.", ephemeral=True)
            return

        reopened_payload = create_v2_payload(
            f"## 🔓 티켓이 재개되었습니다.\n\n<@{ticket['user_id']}>님이 다시 티켓을 확인하고 대화할 수 있습니다.",
            color=0x32CD32,
            extra_components=[{
                "type": 1,
                "components": [{
                    "type": 2,
                    "style": 4,
                    "label": "티켓 닫기",
                    "custom_id": "ticket_close"
                }]
            }],
        )
        reopened_payload["allowed_mentions"] = {"parse": [], "users": [str(ticket['user_id'])]}
        await interaction.client.http.request(
            discord.http.Route("POST", f"/channels/{interaction.channel.id}/messages"),
            json=reopened_payload,
        )
        await interaction.followup.send("✅ 티켓을 재개하고 티켓 생성자의 접근 권한을 복구했습니다.", ephemeral=True)
        return

    # 티켓 영구 삭제 버튼
    if custom_id == "ticket_delete":
        if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("❌ 관리자만 티켓을 삭제할 수 있습니다.", ephemeral=True)
            return
            
        await interaction.response.defer()
        
        async with bot.db_pool.acquire() as conn:
            ticket = await conn.fetchrow('SELECT * FROM tickets WHERE channel_id = $1', interaction.channel.id)
            settings = await conn.fetchrow('SELECT * FROM guild_settings WHERE guild_id = $1', interaction.guild.id)
            
        if settings and settings['ticket_log_channel_id']:
            log_channel = interaction.guild.get_channel(settings['ticket_log_channel_id'])
            if log_channel:
                transcript = await chat_exporter.export(interaction.channel)
                transcript_file = discord.File(
                    io.BytesIO(transcript.encode()), 
                    filename=f"transcript-{interaction.channel.name}.html"
                )
                
                # 티켓 삭제 로그 (한국 시간)
                now_str = datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
                creator_id = ticket['user_id'] if ticket else "알 수 없음"
                
                log_content = (
                    "## 🗑️ 티켓 삭제 로그\n\n"
                    "`👤`**티켓 연 사람**\n"
                    f"<@{creator_id}>\n\n"
                    "`👑`**티켓 삭제한 사람**\n"
                    f"{interaction.user.mention}\n\n"
                    "`🕒`**삭제 시각**\n"
                    f"`{now_str}`\n\n"
                    "아래 첨부된 HTML 파일을 다운로드하여 브라우저로 열면 대화 내용을 디스코드와 똑같은 화면으로 확인하실 수 있습니다."
                )
                
                log_payload = {
                    "flags": 1 << 15, 
                    "components": [
                        {
                            "type": 17, 
                            "accent_color": 0x32CD32, 
                            "components": [
                                {
                                    "type": 10, 
                                    "content": log_content
                                }
                            ]
                        }
                    ]
                }
                
                await interaction.client.http.request(
                    discord.http.Route("POST", f"/channels/{log_channel.id}/messages"), 
                    json=log_payload
                )
                
                await log_channel.send(file=transcript_file)
                
        await interaction.channel.delete()
        return

    # 결제하기 버튼 로직
    if custom_id.startswith("pay_"):
        if not bot.db_pool:
            await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
            return
        order_id = custom_id.removeprefix("pay_")
        async with bot.db_pool.acquire() as conn:
            order = await conn.fetchrow('SELECT * FROM orders WHERE order_id = $1 AND guild_id = $2', order_id, interaction.guild.id)
            settings = await conn.fetchrow('SELECT * FROM guild_settings WHERE guild_id = $1', interaction.guild.id)
            balance = await get_balance(conn, interaction.guild.id, interaction.user.id)
        if not order or order['buyer_id'] != interaction.user.id:
            await interaction.response.send_message("❌ 이 주문의 구매자만 결제할 수 있습니다.", ephemeral=True)
            return
        if order['status'] != 'PENDING' or order['payment_requested']:
            await interaction.response.send_message("❌ 이미 결제를 요청했거나 처리된 주문입니다.", ephemeral=True)
            return
        if not settings or not settings.get('approval_channel_id'):
            await interaction.response.send_message("❌ 관리자가 결제 승인 채널을 설정해야 합니다.", ephemeral=True)
            return
        try:
            modal = DepositModal(order_id, dict(order), dict(settings), balance)
        except PointsError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        await interaction.response.send_modal(modal)
        return

    # 결제 승인/거절 버튼 처리
    if custom_id.startswith("approve_") or custom_id.startswith("reject_"):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ 관리자만 결제를 승인하거나 거절할 수 있습니다.", ephemeral=True)
            return
        if not bot.db_pool:
            await interaction.response.send_message("❌ DB 오류가 발생했습니다.", ephemeral=True)
            return
        action, order_id = custom_id.split("_", 1)
        await interaction.response.defer()
        async with bot.db_pool.acquire() as conn:
            settings = await conn.fetchrow('SELECT * FROM guild_settings WHERE guild_id = $1', interaction.guild.id)
            try:
                order = await resolve_payment(conn, order_id, interaction.guild.id, action == 'approve')
            except PointsError as exc:
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)
                return
            if action == 'approve':
                amount_int = parse_amount(order['amount'])
                user_info_row = await conn.fetchrow('SELECT total_spent FROM user_info WHERE user_id = $1', order['buyer_id'])
                current_total = user_info_row['total_spent'] if user_info_row else amount_int
                member = interaction.guild.get_member(order['buyer_id'])
                
                if member and current_total >= 1 and settings and settings.get('buyer_role_id'):
                    b_role = interaction.guild.get_role(settings['buyer_role_id'])
                    if b_role and b_role not in member.roles:
                        try: 
                            await member.add_roles(b_role)
                        except Exception as e: 
                            pass

                tiers = await conn.fetch('SELECT role_id, required_amount FROM vip_tiers WHERE guild_id = $1 ORDER BY required_amount DESC', interaction.guild.id)
                target_role_id = None
                
                for t in tiers:
                    if current_total >= t['required_amount']:
                        target_role_id = t['role_id']
                        break
                        
                if target_role_id and member:
                    role = interaction.guild.get_role(target_role_id)
                    if role and role not in member.roles:
                        try:
                            await member.add_roles(role)
                            
                            if settings and settings.get('tier_log_channel_id'):
                                tier_channel = interaction.guild.get_channel(settings['tier_log_channel_id'])
                                if tier_channel:
                                    tier_payload = create_tier_upgrade_payload(
                                        order['buyer_id'], role.mention, current_total
                                    )
                                    
                                    await interaction.client.http.request(
                                        discord.http.Route("POST", f"/channels/{tier_channel.id}/messages"),
                                        json=tier_payload
                                    )
                        except Exception as e: 
                            pass

                await update_leaderboard(interaction.guild.id)

                # 구매자 상세 배송 정보를 설정된 구매자정보채널로 전송 + 멘션
                if settings and settings.get('buyer_info_channel_id'):
                    buyer_info_ch = interaction.guild.get_channel(settings['buyer_info_channel_id'])
                    if buyer_info_ch:
                        buyer_user_data = await conn.fetchrow('SELECT * FROM user_info WHERE user_id = $1', order['buyer_id'])
                        
                        b_name = buyer_user_data['name'] if buyer_user_data and buyer_user_data['name'] else "미등록"
                        b_contact = buyer_user_data['contact'] if buyer_user_data and buyer_user_data['contact'] else "미등록"
                        b_address = buyer_user_data['address'] if buyer_user_data and buyer_user_data['address'] else "미등록"
                        b_cvs = buyer_user_data['cvs'] if buyer_user_data and buyer_user_data['cvs'] else "X"
                        
                        formatted_prod_info = "\n".join([f"`{p.strip()}`" for p in order['product'].split(",") if p.strip()])
                        now_kst_str = datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
                        
                        buyer_info_txt = (
                            "## 📋 구매자 상세 배송 정보\n\n"
                            "<@259758966043574272>\n\n"
                            f"`👤` **구매자:** <@{order['buyer_id']}>\n"
                            f"`🕒` **구매일시:** `{now_kst_str}`\n"
                            f"`🧾` **주문번호:** `{order_id}`\n"
                            f"`👤` **성함:** `{b_name}`\n"
                            f"`📞` **연락처:** `{b_contact}`\n"
                            f"`🏠` **주소:** `{b_address}`\n"
                            f"`🏪` **편의점:** `{b_cvs}`\n"
                            f"`📦` **상품:**\n{formatted_prod_info}\n\n"
                            f"`💰` **금액:** `{order['amount']}`"
                        )
                        
                        buyer_info_payload = create_v2_payload(buyer_info_txt)
                        buyer_info_payload["allowed_mentions"] = {
                            "parse": [],
                            "users": ["259758966043574272", str(order['buyer_id'])],
                        }
                        
                        try:
                            await interaction.client.http.request(
                                discord.http.Route("POST", f"/channels/{buyer_info_ch.id}/messages"),
                                json=buyer_info_payload
                            )
                        except Exception as e:
                            print(f"구매자 정보 채널 전송 실패: {e}")

        # 승인/거절 처리 시간 (한국 시간)
        now_str = datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        buyer_mention = f"<@{order['buyer_id']}>"
        anonymous_role_id = settings.get('anonymous_role_id') if settings else None
        status_word, color_val = ("✅ 승인", 0x32CD32) if action == 'approve' else ("❌ 거절", 0xFF0000)
        
        depositor_name = order.get('depositor_name') or "알 수 없음"
        formatted_product = "\n".join([f"`{p.strip()}`" for p in order['product'].split(",") if p.strip()])
        
        admin_updated = (
            f"## {status_word} 완료\n\n"
            "`👤`**구매자**\n"
            f"{buyer_mention}\n\n"
            "`✍️`**입금자명**\n"
            f"`{depositor_name}`\n\n"
            "`◾`**주문번호**\n"
            f"`{order_id}`\n\n"
            "`📦`**상품**\n"
            f"{formatted_product}\n\n"
            "`◾`**수량**\n"
            f"`{order['quantity']}`\n\n"
            "`💰`**금액**\n"
            f"{payment_summary(order)}\n\n"
            "`👑`**처리자**\n"
            f"{interaction.user.mention}\n\n"
            "`🕒`**처리시각**\n"
            f"`{now_str}`"
        )
        
        update_payload = {
            "flags": 1 << 15, 
            "allowed_mentions": {
                "parse": [],
                "users": [str(order['buyer_id']), str(interaction.user.id)],
            },
            "components": [
                {
                    "type": 17, 
                    "accent_color": color_val, 

                    "components": [
                        {
                            "type": 10, 
                            "content": admin_updated
                        }
                    ]
                }
            ]
        }
        
        await interaction.client.http.request(
            discord.http.Route("PATCH", f"/webhooks/{interaction.application_id}/{interaction.token}/messages/@original"),
            json=update_payload
        )

        if action == 'approve':
            if settings and settings['log_channel_id']:
                log_payload = build_purchase_log_payload(order, anonymous_role_id)
                log_response = await interaction.client.http.request(
                    discord.http.Route("POST", f"/channels/{settings['log_channel_id']}/messages"), 
                    json=log_payload
                )
                if log_response and log_response.get('id'):
                    async with bot.db_pool.acquire() as conn:
                        await conn.execute(
                            """
                            UPDATE orders
                            SET purchase_log_channel_id = $3, purchase_log_message_id = $4
                            WHERE order_id = $1 AND guild_id = $2
                            """,
                            order_id,
                            interaction.guild.id,
                            settings['log_channel_id'],
                            int(log_response['id']),
                        )

            noti_txt = (
                "## VAPE SP0T Order Complete\n\n"
                f"{buyer_mention}님의 주문이 승인되었습니다.\n"
                "`아래 정보를 확인하신 후 입금을 진행해 주세요.`\n\n"
                "`🧾`주문정보\n\n"
                "`◾`**주문번호**\n"
                f"`{order_id}`\n\n"
                "`📦`**상품**\n"
                f"{formatted_product}\n\n"
                "`💰`**금액**\n"
                f"{payment_summary(order)}\n\n"
                "```감사합니다.```"
            )
            
            noti_payload = {
                "flags": 1 << 15, 
                "allowed_mentions": {"parse": [], "users": [str(order['buyer_id'])]},
                "components": [
                    {
                        "type": 17, 
                        "accent_color": 0x32CD32, 
                        "components": [
                            {
                                "type": 10, 
                                "content": noti_txt
                            }
                        ]
                    }
                ]
            }
            
            await interaction.client.http.request(
                discord.http.Route("POST", f"/channels/{order['original_channel_id']}/messages"), 
                json=noti_payload
            )
