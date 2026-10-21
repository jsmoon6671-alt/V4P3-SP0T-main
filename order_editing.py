"""관리자가 주문과 연결된 구매로그·누적 구매액을 함께 수정하는 기능."""

import datetime as dt
import discord
from discord import app_commands

from loyalty_points import PointsError, parse_amount, payment_summary


KST = dt.timezone(dt.timedelta(hours=9))


class OrderEditError(Exception):
    """주문 수정 요청을 처리할 수 없을 때 사용하는 오류."""


def parse_points_allowed(value: str) -> bool:
    normalized = "".join(value.lower().split())
    if normalized in {"가능", "사용가능", "예", "네", "true", "yes", "1"}:
        return True
    if normalized in {"불가능", "사용불가능", "아니요", "아니오", "false", "no", "0"}:
        return False
    raise OrderEditError("포인트 사용 여부는 `가능` 또는 `불가능`으로 입력해 주세요.")


def _existing_order(order) -> dict:
    if order is None:
        raise OrderEditError("해당 서버에서 주문번호를 찾을 수 없습니다.")
    return dict(order)


async def get_editable_order(conn, order_id: str, guild_id: int) -> dict:
    order = await conn.fetchrow(
        "SELECT * FROM orders WHERE order_id = $1 AND guild_id = $2",
        order_id,
        guild_id,
    )
    return _existing_order(order)


async def update_order(
    conn,
    order_id: str,
    guild_id: int,
    product: str,
    quantity: str,
    amount: str,
    points_allowed_text: str,
    depositor_name: str,
) -> tuple[dict, dict]:
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
        order = _existing_order(order)
        old_order = dict(order)
        old_amount = parse_amount(order["amount"])
        points_used = int(order.get("points_used") or 0)
        payment_requested = bool(order.get("payment_requested"))

        if payment_requested and points_allowed != bool(order.get("points_allowed", True)):
            raise OrderEditError(
                "결제 요청이 끝난 주문은 포인트 사용 가능 여부를 바꿀 수 없습니다. "
                "기존 값은 그대로 두고 다시 제출해 주세요."
            )
        if payment_requested and parse_amount(normalized_amount) < points_used:
            raise OrderEditError(
                f"변경 가격은 이미 사용한 포인트 `{points_used:,}P`보다 작을 수 없습니다."
            )

        new_amount = parse_amount(normalized_amount)
        cash_amount = new_amount - points_used if payment_requested else None
        await conn.execute(
            """
            UPDATE orders
            SET product = $3,
                quantity = $4,
                amount = $5,
                points_allowed = $6,
                depositor_name = $7,
                cash_amount = $8
            WHERE order_id = $1 AND guild_id = $2
            """,
            order_id,
            guild_id,
            product,
            quantity,
            normalized_amount,
            points_allowed,
            depositor_name,
            cash_amount,
        )

        if order.get("status") == "APPROVED" and new_amount != old_amount:
            delta = new_amount - old_amount
            await conn.execute(
                """
                INSERT INTO user_info (user_id, total_spent)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE
                SET total_spent = GREATEST(COALESCE(user_info.total_spent, 0) + $3, 0)
                """,
                order["buyer_id"],
                new_amount,
                delta,
            )

    order.update(
        product=product,
        quantity=quantity,
        amount=normalized_amount,
        points_allowed=points_allowed,
        depositor_name=depositor_name,
        cash_amount=cash_amount,
    )
    return old_order, order


def _as_datetime(value) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        result = value
    else:
        try:
            result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=dt.timezone.utc)
    return result


def build_purchase_log_payload(order: dict, anonymous_role_id: int | None) -> dict:
    buyer = f"<@&{anonymous_role_id}>" if anonymous_role_id else "**익명**"
    products = "\n".join(
        f"`{_display(product.strip())}`"
        for product in str(order["product"]).split(",")
        if product.strip()
    )
    purchased_at = _as_datetime(order.get("processed_at") or order.get("created_at"))
    purchased_at = purchased_at.astimezone(KST) if purchased_at else dt.datetime.now(KST)
    time_text = purchased_at.strftime("%Y-%m-%d %H:%M:%S")
    content = (
        "## VAPE SP0T Purchase Log\n\n"
        "```- 구매가 완료되었습니다.\n"
        "> 즐거운 흡연 되시길 바랍니다.```\n\n"
        "`🧾`구매정보\n\n"
        "`👤`**구매자**\n"
        f"{buyer}\n\n"
        "`◾`**주문번호**\n"
        f"`{_display(order['order_id'])}`\n\n"
        "`📦`**상품**\n"
        f"{products}\n\n"
        "`◾`**수량**\n"
        f"`{_display(order['quantity'])}`\n\n"
        "`💰`**금액**\n"
        f"{payment_summary(order)}\n\n"
        "`🕒`**구매시각**\n"
        f"`{time_text}`\n\n"
        "**```믿고 구매해 주셔서 감사합니다.```**"
    )
    return {
        "flags": 1 << 15,
        "allowed_mentions": (
            {"parse": [], "roles": [str(anonymous_role_id)]}
            if anonymous_role_id else {"parse": []}
        ),
        "components": [{
            "type": 17,
            "accent_color": 0x32CD32,
            "components": [{"type": 10, "content": content}],
        }],
    }


def _component_content(value) -> str:
    if isinstance(value, dict):
        own = value.get("content") if isinstance(value.get("content"), str) else ""
        return own + "\n" + "\n".join(_component_content(item) for item in value.get("components", []))
    if isinstance(value, list):
        return "\n".join(_component_content(item) for item in value)
    return ""


def _legacy_log_matches(message: dict, order: dict) -> bool:
    content = _component_content(message.get("components", []))
    products = [part.strip() for part in str(order["product"]).split(",") if part.strip()]
    return (
        "VAPE SP0T Purchase Log" in content
        and all(f"`{product}`" in content for product in products)
        and f"`{order['quantity']}`" in content
        and payment_summary(order) in content
    )


async def find_legacy_purchase_log(http, channel_id: int, order: dict, maximum: int = 1000) -> int | None:
    candidates = []
    before = None
    checked = 0
    while checked < maximum:
        params = {"limit": min(100, maximum - checked)}
        if before:
            params["before"] = before
        messages = await http.request(
            discord.http.Route("GET", f"/channels/{channel_id}/messages"),
            params=params,
        )
        if not messages:
            break
        checked += len(messages)
        candidates.extend(message for message in messages if _legacy_log_matches(message, order))
        if len(messages) < params["limit"]:
            break
        before = messages[-1]["id"]

    if not candidates:
        return None
    created_at = _as_datetime(order.get("created_at"))
    if created_at:
        candidates.sort(key=lambda message: (
            discord.utils.snowflake_time(int(message["id"])) < created_at,
            abs((discord.utils.snowflake_time(int(message["id"])) - created_at).total_seconds()),
        ))
    return int(candidates[0]["id"])


async def update_purchase_log(bot, guild_id: int, old_order: dict, order: dict) -> bool:
    if old_order.get("status") != "APPROVED":
        return True
    async with bot.db_pool.acquire() as conn:
        settings = await conn.fetchrow(
            "SELECT log_channel_id, anonymous_role_id FROM guild_settings WHERE guild_id = $1",
            guild_id,
        )
    settings = dict(settings) if settings else {}
    channel_id = order.get("purchase_log_channel_id") or settings.get("log_channel_id")
    if not channel_id:
        return False
    message_id = order.get("purchase_log_message_id")
    if not message_id:
        message_id = await find_legacy_purchase_log(bot.http, int(channel_id), old_order)
        if not message_id:
            return False
        async with bot.db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE orders SET purchase_log_channel_id = $3, purchase_log_message_id = $4
                WHERE order_id = $1 AND guild_id = $2
                """,
                order["order_id"], guild_id, int(channel_id), int(message_id),
            )
        order["purchase_log_channel_id"] = int(channel_id)
        order["purchase_log_message_id"] = int(message_id)

    await bot.http.request(
        discord.http.Route("PATCH", f"/channels/{int(channel_id)}/messages/{int(message_id)}"),
        json=build_purchase_log_payload(order, settings.get("anonymous_role_id")),
    )
    return True


async def sync_buyer_roles(bot, guild, buyer_id: int) -> bool:
    member = guild.get_member(buyer_id)
    if member is None:
        try:
            member = await guild.fetch_member(buyer_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False
    async with bot.db_pool.acquire() as conn:
        settings = await conn.fetchrow(
            "SELECT buyer_role_id FROM guild_settings WHERE guild_id = $1",
            guild.id,
        )
        tiers = await conn.fetch(
            "SELECT role_id, required_amount FROM vip_tiers WHERE guild_id = $1 ORDER BY required_amount DESC",
            guild.id,
        )
        total = int(await conn.fetchval(
            "SELECT total_spent FROM user_info WHERE user_id = $1", buyer_id,
        ) or 0)

    try:
        buyer_role_id = settings.get("buyer_role_id") if settings else None
        buyer_role = guild.get_role(buyer_role_id) if buyer_role_id else None
        if buyer_role:
            if total > 0 and buyer_role not in member.roles:
                await member.add_roles(buyer_role, reason="주문 수정 후 구매 역할 동기화")
            elif total <= 0 and buyer_role in member.roles:
                await member.remove_roles(buyer_role, reason="주문 수정 후 구매 역할 동기화")

        target_role_id = next(
            (int(tier["role_id"]) for tier in tiers if total >= int(tier["required_amount"])),
            None,
        )
        for tier in tiers:
            role = guild.get_role(int(tier["role_id"]))
            if not role:
                continue
            if role.id == target_role_id and role not in member.roles:
                await member.add_roles(role, reason="주문 수정 후 등급 재계산")
            elif role.id != target_role_id and role in member.roles:
                await member.remove_roles(role, reason="주문 수정 후 등급 재계산")
    except (discord.Forbidden, discord.HTTPException):
        return False
    return True


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
    def __init__(self, bot, order: dict, refresh_leaderboard):
        super().__init__(title=f"주문 수정 · {order['order_id']}"[:45])
        self.bot = bot
        self.order_id = str(order["order_id"])
        self.guild_id = int(order["guild_id"])
        self.refresh_leaderboard = refresh_leaderboard

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
                old_order, order = await update_order(
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

        warnings = []
        if old_order.get("status") == "APPROVED":
            try:
                if not await update_purchase_log(self.bot, self.guild_id, old_order, order):
                    warnings.append("기존 구매로그 메시지를 찾지 못해 로그는 자동 수정되지 않았습니다.")
            except Exception:
                warnings.append("Discord 오류로 구매로그 메시지를 수정하지 못했습니다.")

            try:
                roles_ok = await sync_buyer_roles(self.bot, interaction.guild, int(order["buyer_id"]))
            except Exception:
                roles_ok = False
            if not roles_ok:
                warnings.append("구매자의 등급 역할을 확인하지 못했습니다. 봇 역할 권한을 확인해 주세요.")
            try:
                await self.refresh_leaderboard(self.guild_id)
            except Exception:
                warnings.append("구매 랭킹 패널을 갱신하지 못했습니다.")

        point_text = "가능" if order["points_allowed"] else "불가능"
        depositor = _display(order.get("depositor_name") or "미입력")
        warning_text = "" if not warnings else "\n\n⚠️ " + "\n⚠️ ".join(warnings)
        await interaction.followup.send(
            "✅ 주문을 수정했습니다.\n\n"
            f"주문번호: `{_display(order['order_id'])}`\n"
            f"상품: `{_display(order['product'])}`\n"
            f"수량: `{_display(order['quantity'])}`\n"
            f"가격: `{_display(order['amount'])}`\n"
            f"포인트 사용: `{point_text}`\n"
            f"입금자명: `{depositor}`"
            f"{warning_text}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def register_order_edit_command(bot, refresh_leaderboard):
    @bot.tree.command(name="주문수정", description="주문과 구매로그의 상품, 수량, 가격 등을 수정합니다. (관리자 전용)")
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
            modal = OrderEditModal(bot, order, refresh_leaderboard)
        except OrderEditError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        await interaction.response.send_modal(modal)

    return edit_order_command
