"""관리자가 결제 요청 전 주문을 안전하게 수정하는 슬래시 명령어."""

import discord
from discord import app_commands

from loyalty_points import PointsError, parse_amount


class OrderEditError(Exception):
    """주문 수정 요청을 처리할 수 없을 때 사용하는 오류."""


def parse_points_allowed(value: str) -> bool:
    normalized = "".join(value.lower().split())
    if normalized in {"가능", "사용가능", "예", "네", "true", "yes", "1"}:
        return True
    if normalized in {"불가능", "사용불가능", "아니요", "아니오", "false", "no", "0"}:
        return False
    raise OrderEditError("포인트 사용 여부는 `가능` 또는 `불가능`으로 입력해 주세요.")


def _editable_order(order) -> dict:
    if order is None:
        raise OrderEditError("해당 서버에서 주문번호를 찾을 수 없습니다.")
    result = dict(order)
    if result.get("status") != "PENDING" or result.get("payment_requested"):
        raise OrderEditError(
            "결제 요청 전의 대기 주문만 수정할 수 있습니다. "
            "이미 결제를 요청했거나 처리된 주문은 수정할 수 없습니다."
        )
    return result


async def get_editable_order(conn, order_id: str, guild_id: int) -> dict:
    order = await conn.fetchrow(
        "SELECT * FROM orders WHERE order_id = $1 AND guild_id = $2",
        order_id,
        guild_id,
    )
    return _editable_order(order)


async def update_order(
    conn,
    order_id: str,
    guild_id: int,
    product: str,
    quantity: str,
    amount: str,
    points_allowed_text: str,
    depositor_name: str,
) -> dict:
    product = product.strip()
    quantity = quantity.strip()
    if not product:
        raise OrderEditError("상품을 입력해 주세요.")
    if not quantity:
        raise OrderEditError("수량을 입력해 주세요.")
    try:
        normalized_amount = f"{parse_amount(amount):,}원"
    except PointsError as exc:
        raise OrderEditError(str(exc)) from exc
    points_allowed = parse_points_allowed(points_allowed_text)
    depositor_name = depositor_name.strip() or None

    async with conn.transaction():
        order = await conn.fetchrow(
            "SELECT * FROM orders WHERE order_id = $1 AND guild_id = $2 FOR UPDATE",
            order_id,
            guild_id,
        )
        order = _editable_order(order)
        await conn.execute(
            """
            UPDATE orders
            SET product = $3,
                quantity = $4,
                amount = $5,
                points_allowed = $6,
                depositor_name = $7
            WHERE order_id = $1 AND guild_id = $2
            """,
            order_id,
            guild_id,
            product,
            quantity,
            normalized_amount,
            points_allowed,
            depositor_name,
        )

    order.update(
        product=product,
        quantity=quantity,
        amount=normalized_amount,
        points_allowed=points_allowed,
        depositor_name=depositor_name,
    )
    return order


def _modal_default(value, maximum: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) > maximum:
        raise OrderEditError("기존 주문 내용이 너무 길어 수정 폼에 표시할 수 없습니다.")
    return text


def _display(value) -> str:
    return str(value).replace("`", "ˋ")


class OrderEditModal(discord.ui.Modal):
    def __init__(self, bot, order: dict):
        super().__init__(title=f"주문 수정 · {order['order_id']}"[:45])
        self.bot = bot
        self.order_id = str(order["order_id"])
        self.guild_id = int(order["guild_id"])

        self.product = discord.ui.TextInput(
            label="상품",
            style=discord.TextStyle.paragraph,
            default=_modal_default(order.get("product"), 4000),
            required=True,
            max_length=4000,
        )
        self.quantity = discord.ui.TextInput(
            label="수량",
            default=_modal_default(order.get("quantity"), 200),
            required=True,
            max_length=200,
        )
        self.amount = discord.ui.TextInput(
            label="가격",
            placeholder="숫자 또는 10,000원 형식으로 입력해 주세요.",
            default=_modal_default(order.get("amount"), 50),
            required=True,
            max_length=50,
        )
        self.points_allowed = discord.ui.TextInput(
            label="포인트 사용 여부",
            placeholder="가능 또는 불가능",
            default="가능" if order.get("points_allowed", True) else "불가능",
            required=True,
            max_length=10,
        )
        self.depositor_name = discord.ui.TextInput(
            label="입금자명 (선택)",
            default=_modal_default(order.get("depositor_name"), 100),
            required=False,
            max_length=100,
        )
        for item in (
            self.product,
            self.quantity,
            self.amount,
            self.points_allowed,
            self.depositor_name,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        permissions = getattr(interaction.user, "guild_permissions", None)
        if (
            interaction.guild is None
            or interaction.guild.id != self.guild_id
            or not permissions
            or not permissions.administrator
        ):
            await interaction.response.send_message("❌ 서버 관리자만 주문을 수정할 수 있습니다.", ephemeral=True)
            return
        if self.bot.db_pool is None:
            await interaction.response.send_message("❌ 데이터베이스가 연결되지 않았습니다.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            async with self.bot.db_pool.acquire() as conn:
                order = await update_order(
                    conn,
                    self.order_id,
                    self.guild_id,
                    self.product.value,
                    self.quantity.value,
                    self.amount.value,
                    self.points_allowed.value,
                    self.depositor_name.value,
                )
        except OrderEditError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        point_text = "가능" if order["points_allowed"] else "불가능"
        depositor = _display(order.get("depositor_name") or "미입력")
        await interaction.followup.send(
            "✅ 주문을 수정했습니다.\n\n"
            f"주문번호: `{_display(order['order_id'])}`\n"
            f"상품: `{_display(order['product'])}`\n"
            f"수량: `{_display(order['quantity'])}`\n"
            f"가격: `{_display(order['amount'])}`\n"
            f"포인트 사용: `{point_text}`\n"
            f"입금자명: `{depositor}`",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def register_order_edit_command(bot):
    @bot.tree.command(name="주문수정", description="결제 요청 전 주문의 상품, 수량, 가격 등을 수정합니다. (관리자 전용)")
    @app_commands.describe(주문번호="수정할 주문번호를 정확히 입력해 주세요.")
    async def edit_order_command(interaction: discord.Interaction, 주문번호: str):
        permissions = getattr(interaction.user, "guild_permissions", None)
        if interaction.guild is None or not permissions or not permissions.administrator:
            await interaction.response.send_message("❌ 서버 관리자만 주문을 수정할 수 있습니다.", ephemeral=True)
            return
        if bot.db_pool is None:
            await interaction.response.send_message("❌ 데이터베이스가 연결되지 않았습니다.", ephemeral=True)
            return

        order_id = 주문번호.strip()
        if not order_id:
            await interaction.response.send_message("❌ 주문번호를 입력해 주세요.", ephemeral=True)
            return
        try:
            async with bot.db_pool.acquire() as conn:
                order = await get_editable_order(conn, order_id, interaction.guild.id)
            modal = OrderEditModal(bot, order)
        except OrderEditError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        await interaction.response.send_modal(modal)

    return edit_order_command
