"""웹 주문의 배송 진행상황, 운송장 발급, 구매자 알림을 관리합니다."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from delivery_tracking import DeliveryTrackingError, normalize_waybill, track_shipment


LOGGER = logging.getLogger(__name__)

PROGRESS = {
    "PAYMENT_APPROVED": ("승인완료", 20),
    "PRODUCT_PREPARING": ("상품준비중", 40),
    "SHIPPING_PREPARING": ("배송준비중", 60),
    "SHIPPING": ("배송중", 80),
    "DELIVERED": ("배송완료", 100),
}

STATUS_CHOICES = [
    discord.SelectOption(label="상품준비중", value="PRODUCT_PREPARING", emoji="📦"),
    discord.SelectOption(label="배송준비중", value="SHIPPING_PREPARING", emoji="🧾"),
    discord.SelectOption(label="배송중", value="SHIPPING", emoji="🚚"),
    discord.SelectOption(label="배송완료", value="DELIVERED", emoji="✅"),
]

CARRIER_CHOICES = [
    app_commands.Choice(name="GS편의점택배", value="kr.cvsnet"),
    app_commands.Choice(name="CU알뜰택배", value="kr.cupost"),
    app_commands.Choice(name="CJ대한통운", value="kr.cjlogistics"),
    app_commands.Choice(name="우체국택배", value="kr.epost"),
]


def _container(content: str, *, color: int = 0x32CD32) -> dict:
    return {
        "flags": 1 << 15,
        "components": [{
            "type": 17,
            "accent_color": color,
            "components": [{"type": 10, "content": content}],
        }],
    }


async def send_user_container(bot, user_id: int, content: str, *, color: int = 0x32CD32):
    user = bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id))
    dm = await user.create_dm()
    await bot.http.request(
        discord.http.Route("POST", f"/channels/{dm.id}/messages"),
        json=_container(content, color=color),
    )


def infer_tracking_progress(data: dict) -> str | None:
    pieces = [str(data.get("status") or "")]
    for item in (data.get("allProgress") or [])[-5:]:
        if not isinstance(item, dict):
            continue
        for key in ("status", "description"):
            value = item.get(key)
            if isinstance(value, dict):
                value = value.get("text") or value.get("name") or ""
            pieces.append(str(value or ""))
    text = " ".join(pieces).lower().replace(" ", "")
    if any(word in text for word in ("배송완료", "배달완료", "전달완료", "delivered")):
        return "DELIVERED"
    if any(word in text for word in ("배송중", "배달중", "이동중", "간선상차", "간선하차", "출고")):
        return "SHIPPING"
    if any(word in text for word in ("집화", "상품인수", "접수", "운송장등록")):
        return "SHIPPING_PREPARING"
    return None


async def update_progress(bot, guild_id: int, order_id: str, status: str, *, allow_regression: bool = True):
    if status not in PROGRESS or status == "PAYMENT_APPROVED":
        raise ValueError("지원하지 않는 진행상황입니다.")
    async with bot.db_pool.acquire() as conn:
        async with conn.transaction():
            order = await conn.fetchrow(
                """
                SELECT order_id, buyer_id, product, fulfillment_status
                FROM orders
                WHERE order_id=$1 AND guild_id=$2 AND source='WEB' AND status='APPROVED'
                FOR UPDATE
                """,
                order_id, guild_id,
            )
            if not order:
                raise ValueError("승인된 웹 주문을 찾을 수 없습니다.")
            current = order["fulfillment_status"] or "PAYMENT_APPROVED"
            if current == status:
                return dict(order), False
            if not allow_regression and PROGRESS[status][1] <= PROGRESS.get(current, ("", 0))[1]:
                return dict(order), False
            await conn.execute(
                """
                UPDATE orders
                SET fulfillment_status=$3, fulfillment_updated_at=CURRENT_TIMESTAMP
                WHERE order_id=$1 AND guild_id=$2
                """,
                order_id, guild_id, status,
            )
    label, percent = PROGRESS[status]
    content = (
        "## 📦 주문 진행상황이 변경되었습니다\n\n"
        f"`◼️` **주문번호** : `{order_id}`\n"
        f"`◼️` **상품** : `{order['product']}`\n"
        f"`◼️` **현재 상태** : **{label}**\n"
        f"`◼️` **진행률** : **{percent}%**"
    )
    if status == "DELIVERED":
        content += (
            "\n\n상품은 잘 받아보셨나요?\n"
            "**`/후기작성`** 명령어로 후기와 사진을 남겨 주세요!\n"
            "> 후기 작성 시 랜덤으로 **100P ~ 500P**가 적립됩니다."
        )
    try:
        await send_user_container(bot, order["buyer_id"], content)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        LOGGER.info("웹 주문 진행상황 DM 전송 실패: order=%s user=%s", order_id, order["buyer_id"])
    return {**dict(order), "fulfillment_status": status}, True


class WebProgressSelect(discord.ui.Select):
    def __init__(self, bot, order_id: str):
        self.bot = bot
        self.order_id = order_id
        super().__init__(placeholder="변경할 진행상황을 선택해 주세요", options=STATUS_CHOICES)

    async def callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ 관리자만 변경할 수 있습니다.", ephemeral=True)
            return
        try:
            _, changed = await update_progress(
                self.bot, interaction.guild.id, self.order_id, self.values[0], allow_regression=True,
            )
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        label, percent = PROGRESS[self.values[0]]
        if not changed:
            await interaction.response.edit_message(
                content=f"ℹ️ `{self.order_id}` 주문은 이미 **{label} ({percent}%)** 상태입니다.",
                view=None,
            )
            return
        await interaction.response.edit_message(
            content=f"✅ `{self.order_id}` 주문을 **{label} ({percent}%)** 상태로 변경하고 구매자에게 알렸습니다.",
            view=None,
        )


class WebProgressView(discord.ui.View):
    def __init__(self, bot, order_id: str):
        super().__init__(timeout=300)
        self.add_item(WebProgressSelect(bot, order_id))


class WebOrderNumberModal(discord.ui.Modal, title="웹 주문 진행상황 변경"):
    order_id = discord.ui.TextInput(
        label="주문번호",
        placeholder="WEB-로 시작하는 주문번호를 입력해 주세요.",
        max_length=50,
    )

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        value = self.order_id.value.strip().upper()
        async with self.bot.db_pool.acquire() as conn:
            order = await conn.fetchrow(
                """
                SELECT order_id, buyer_id, product, fulfillment_status
                FROM orders
                WHERE order_id=$1 AND guild_id=$2 AND source='WEB' AND status='APPROVED'
                """,
                value, interaction.guild.id,
            )
        if not order:
            await interaction.response.send_message("❌ 승인된 웹 주문번호를 찾을 수 없습니다.", ephemeral=True)
            return
        current = order["fulfillment_status"] or "PAYMENT_APPROVED"
        label, percent = PROGRESS.get(current, (current, 0))
        await interaction.response.send_message(
            f"**상품:** {order['product']}\n**구매자:** <@{order['buyer_id']}>\n**현재 상태:** {label} ({percent}%)",
            view=WebProgressView(self.bot, value),
            ephemeral=True,
        )


async def handle_web_progress_interaction(interaction: discord.Interaction, bot) -> bool:
    custom_id = (interaction.data or {}).get("custom_id", "")
    if custom_id != "web_progress_open":
        return False
    if interaction.guild is None or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return True
    await interaction.response.send_modal(WebOrderNumberModal(bot))
    return True


def register_web_order_commands(bot):
    @bot.tree.command(name="웹진행상황패널", description="웹 주문 진행상황을 변경하는 관리자 패널을 전송합니다.")
    async def web_progress_panel(interaction: discord.Interaction):
        payload = _container(
            "## 📦 웹 주문 진행상황 관리\n\n"
            "아래 버튼을 눌러 웹 주문번호를 입력한 뒤 진행상황을 변경해 주세요.\n"
            "변경하면 웹 주문내역의 진행률이 갱신되고 구매자에게 DM이 전송됩니다."
        )
        payload["components"][0]["components"].append({
            "type": 1,
            "components": [{
                "type": 2,
                "style": 3,
                "label": "진행상황 변경",
                "custom_id": "web_progress_open",
                "emoji": {"name": "📦"},
            }],
        })
        await interaction.response.send_message("패널을 전송했습니다.", ephemeral=True)
        await interaction.client.http.request(
            discord.http.Route("POST", f"/channels/{interaction.channel_id}/messages"), json=payload,
        )

    @bot.tree.command(name="웹운송장", description="웹 주문에 운송장을 등록하고 구매자에게 안내합니다.")
    @app_commands.describe(
        운송장="발급된 운송장 번호",
        주문번호="WEB-로 시작하는 주문번호",
        택배사="일반택배는 택배사를 선택해 주세요. GS25/CU는 자동 선택됩니다.",
    )
    @app_commands.choices(택배사=CARRIER_CHOICES)
    async def web_waybill(
        interaction: discord.Interaction,
        운송장: str,
        주문번호: str,
        택배사: app_commands.Choice[str] | None = None,
    ):
        try:
            waybill = normalize_waybill(운송장)
        except DeliveryTrackingError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        order_id = 주문번호.strip().upper()
        await interaction.response.defer(ephemeral=True)
        async with bot.db_pool.acquire() as conn:
            order = await conn.fetchrow(
                """
                SELECT order_id, buyer_id, product, shipping_method, fulfillment_status
                FROM orders
                WHERE order_id=$1 AND guild_id=$2 AND source='WEB' AND status='APPROVED'
                """,
                order_id, interaction.guild.id,
            )
            if not order:
                await interaction.followup.send("❌ 승인된 웹 주문번호를 찾을 수 없습니다.", ephemeral=True)
                return
            method = str(order["shipping_method"] or "")
            carrier_id = (
                "kr.cvsnet" if "GS25" in method
                else "kr.cupost" if "CU" in method
                else 택배사.value if 택배사 else None
            )
            progress = order["fulfillment_status"] or "PAYMENT_APPROVED"
            if PROGRESS.get(progress, ("", 0))[1] < PROGRESS["SHIPPING_PREPARING"][1]:
                progress = "SHIPPING_PREPARING"
            if carrier_id:
                try:
                    detected = infer_tracking_progress(await track_shipment(carrier_id, waybill))
                    if detected and PROGRESS[detected][1] > PROGRESS[progress][1]:
                        progress = detected
                except DeliveryTrackingError:
                    pass
            await conn.execute(
                """
                UPDATE orders SET waybill_number=$3, carrier_id=$4,
                    fulfillment_status=$5, fulfillment_updated_at=CURRENT_TIMESTAMP
                WHERE order_id=$1 AND guild_id=$2
                """,
                order_id, interaction.guild.id, waybill, carrier_id, progress,
            )

        label, percent = PROGRESS[progress]
        content = (
            "## 🚚 운송장이 발급되었습니다\n\n"
            f"`◼️` **주문번호** : `{order_id}`\n"
            f"`◼️` **운송장** : `{waybill}`\n"
            f"`◼️` **상품** : `{order['product']}`\n"
            f"`◼️` **진행상황** : **{label} ({percent}%)**\n\n"
            "> 웹사이트 **내 정보 → 주문내역 → 상세조회**에서 배송 현황을 확인할 수 있습니다."
        )
        try:
            await send_user_container(bot, order["buyer_id"], content)
            dm_result = "구매자에게 컨테이너 DM도 전송했습니다."
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            dm_result = "구매자가 DM을 차단하여 DM은 전송하지 못했습니다."
        carrier_result = "자동 배송조회가 시작됩니다." if carrier_id else "일반택배의 택배사를 선택하지 않아 자동 조회는 대기합니다."
        await interaction.followup.send(
            f"✅ `{order_id}`에 운송장을 등록했습니다. {dm_result}\n{carrier_result}", ephemeral=True,
        )


class WebOrderProgressCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.poll_shipments.start()

    def cog_unload(self):
        self.poll_shipments.cancel()

    @tasks.loop(minutes=10)
    async def poll_shipments(self):
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT order_id, guild_id, waybill_number, carrier_id, fulfillment_status
                FROM orders
                WHERE source='WEB' AND status='APPROVED'
                  AND waybill_number IS NOT NULL AND carrier_id IS NOT NULL
                  AND COALESCE(fulfillment_status, 'PAYMENT_APPROVED') <> 'DELIVERED'
                ORDER BY fulfillment_updated_at NULLS FIRST
                LIMIT 50
                """
            )
        for row in rows:
            try:
                data = await track_shipment(row["carrier_id"], row["waybill_number"])
                status = infer_tracking_progress(data)
                if status:
                    await update_progress(
                        self.bot, row["guild_id"], row["order_id"], status, allow_regression=False,
                    )
            except (DeliveryTrackingError, ValueError):
                continue
            except Exception:
                LOGGER.exception("웹 운송장 자동조회 실패: order=%s", row["order_id"])

    @poll_shipments.before_loop
    async def before_poll_shipments(self):
        await self.bot.wait_until_ready()
