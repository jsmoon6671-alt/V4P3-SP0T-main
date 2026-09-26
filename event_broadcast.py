"""관리자가 작성한 이벤트를 서버의 일반 멤버에게 DM으로 보냅니다."""

import asyncio
import logging

import discord
from discord import app_commands


logger = logging.getLogger(__name__)


class EventBroadcastService:
    def __init__(self, bot):
        self.bot = bot
        self.active_guilds = set()
        self.tasks = set()

    async def broadcast(self, interaction, body):
        guild = interaction.guild
        succeeded = failed = skipped = 0
        error = None
        try:
            # 캐시되지 않은 오프라인 멤버도 포함하고, 목록 조회 실패 시 전송을 시작하지 않습니다.
            members = [member async for member in guild.fetch_members(limit=None)]
            embed = discord.Embed(
                title=f"📢 {guild.name} 이벤트",
                description=body,
                color=0x32CD32,
            )
            embed.set_footer(text=f"{guild.name} 서버에서 보낸 이벤트 안내")
            for member in members:
                if member.bot:
                    skipped += 1
                    continue
                try:
                    await member.send(
                        content=f"{member.mention}님, 서버 이벤트를 확인해 주세요!",
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions(
                            everyone=False, roles=False, users=[member], replied_user=False,
                        ),
                    )
                    succeeded += 1
                except discord.Forbidden:
                    failed += 1
                except discord.HTTPException:
                    failed += 1
                # discord.py의 rate-limit 처리와 함께 순차 전송합니다.
                await asyncio.sleep(1.5)
        except discord.ClientException:
            error = "전체 멤버를 조회할 수 없습니다. Developer Portal에서 Server Members Intent를 켜 주세요."
        except discord.HTTPException:
            error = "서버 멤버 목록을 불러오지 못했습니다. 봇의 서버 연결 상태를 확인해 주세요."
        except Exception:
            logger.exception("이벤트 전송 작업 오류 (guild_id=%s)", guild.id)
            error = "전송 중 오류가 발생하여 작업이 중단되었습니다. 봇 로그를 확인해 주세요."
        finally:
            self.active_guilds.discard(guild.id)

        report = (
            f"{'❌ 이벤트 전송 중단' if error else '✅ 이벤트 전송 완료'}\n"
            f"서버: {discord.utils.escape_mentions(discord.utils.escape_markdown(guild.name))}\n"
            f"성공: {succeeded}명 / 실패: {failed}명 / 봇 제외: {skipped}명"
        )
        if error:
            report += f"\n{error}"
        elif failed:
            report += "\nDM 차단·수신 설정 또는 전송 오류로 실패한 멤버가 있습니다."

        # 대규모 서버에서 15분짜리 interaction 토큰이 만료되어도 완료 결과를 알립니다.
        try:
            await interaction.user.send(report, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logger.warning("이벤트 전송 결과 DM 실패 (guild_id=%s, user_id=%s)", guild.id, interaction.user.id)
        if not interaction.is_expired():
            try:
                await interaction.edit_original_response(content=report)
            except discord.HTTPException:
                logger.warning("이벤트 전송 결과 응답 실패 (guild_id=%s)", guild.id)

    def start(self, interaction, body):
        task = asyncio.create_task(self.broadcast(interaction, body))
        self.tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("이벤트 전송 작업 실패", exc_info=(type(task.exception()), task.exception(), task.exception().__traceback__))


class EventModal(discord.ui.Modal, title="서버 이벤트 작성"):
    body = discord.ui.TextInput(
        label="이벤트 내용",
        placeholder="서버 멤버 모두에게 DM으로 보낼 이벤트 내용을 작성해 주세요.",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=4000,
    )

    def __init__(self, service, guild_id, author_id):
        super().__init__(timeout=600)
        self.service = service
        self.guild_id = guild_id
        self.author_id = author_id
        self.submitted = False

    async def on_submit(self, interaction: discord.Interaction):
        if (interaction.guild is None or interaction.guild.id != self.guild_id
                or interaction.user.id != self.author_id
                or not interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("❌ 서버 관리자만 이벤트를 전송할 수 있습니다.", ephemeral=True)
            return
        body = self.body.value.strip()
        if not body:
            await interaction.response.send_message("❌ 이벤트 내용을 입력해 주세요.", ephemeral=True)
            return
        if self.submitted or self.guild_id in self.service.active_guilds:
            await interaction.response.send_message("❌ 이미 이벤트를 전송 중이거나 제출한 폼입니다.", ephemeral=True)
            return
        # 첫 await 전에 서버를 예약하여 동시에 제출한 폼도 중복 전송하지 않습니다.
        self.service.active_guilds.add(self.guild_id)
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await interaction.edit_original_response(
                content="📨 이벤트 DM 전송을 시작합니다. 완료 결과는 이 응답과 관리자님의 DM으로 알려드립니다."
            )
            self.service.start(interaction, body)
            self.submitted = True
        except BaseException:
            self.service.active_guilds.discard(self.guild_id)
            raise


def register_event_command(bot):
    service = EventBroadcastService(bot)
    bot.event_broadcast_service = service

    @bot.tree.command(name="이벤트", description="이벤트 내용을 작성하여 서버 멤버 모두에게 DM으로 전송합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def event_command(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("❌ 서버에서만 사용할 수 있습니다.", ephemeral=True)
            return
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ 서버 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        if interaction.guild.id in service.active_guilds:
            await interaction.response.send_message("❌ 이 서버에서 이벤트 DM을 전송 중입니다.", ephemeral=True)
            return
        await interaction.response.send_modal(EventModal(service, interaction.guild.id, interaction.user.id))
