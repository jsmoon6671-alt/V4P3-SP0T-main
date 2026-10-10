"""승인된 주문의 비공개 구매 티켓을 자동으로 생성한다."""

from __future__ import annotations

import logging
import re

import discord


LOGGER = logging.getLogger(__name__)


def purchase_ticket_channel_name(username: str, order_id: str) -> str:
    """Discord 채널명에 사용할 수 있는 짧고 구분 가능한 이름을 만든다."""
    user_part = re.sub(r"[^0-9a-zA-Z가-힣_-]+", "-", username).strip("-").lower()
    user_part = user_part[:24] or "buyer"
    order_part = re.sub(r"[^0-9a-zA-Z]+", "", order_id).lower()[-8:] or "order"
    return f"구매문의-{user_part}-{order_part}"[:100]


def _ticket_payload(order: dict, buyer_id: int, shipping: dict) -> dict:
    product_lines = "\n".join(
        f"`{part.strip()}`" for part in str(order.get("product") or "미등록").split(",") if part.strip()
    )
    content = (
        "## 🎫 구매티켓이 자동으로 생성되었습니다\n\n"
        f"<@{buyer_id}>님, 주문이 승인되어 관리자와 대화할 수 있는 구매티켓을 열었습니다.\n"
        "배송 및 주문 관련 내용을 이 채널에서 확인해 주세요.\n\n"
        f"`◼️` **주문번호** : `{order['order_id']}`\n"
        f"`◼️` **상품**\n{product_lines}\n"
        f"`◼️` **수량** : `{order.get('quantity') or '미등록'}`\n"
        f"`◼️` **주문금액** : `{order.get('amount') or '미등록'}`\n"
        f"`◼️` **배송방식** : `{shipping.get('method') or '미등록'}`\n"
        f"`◼️` **받는 분** : `{shipping.get('name') or '미등록'}`\n"
        f"`◼️` **연락처** : `{shipping.get('contact') or '미등록'}`\n"
        f"`◼️` **주소 또는 편의점** : `{shipping.get('destination') or '미등록'}`"
    )
    return {
        "flags": 1 << 15,
        "allowed_mentions": {"parse": [], "users": [str(buyer_id)]},
        "components": [{
            "type": 17,
            "accent_color": 0x32CD32,
            "components": [
                {"type": 10, "content": content},
                {"type": 1, "components": [{
                    "type": 2,
                    "style": 4,
                    "label": "티켓 닫기",
                    "custom_id": "ticket_close",
                }]},
            ],
        }],
    }


async def ensure_approved_purchase_ticket(bot, order) -> discord.TextChannel | None:
    """승인 주문에 연결된 구매 티켓을 한 번만 만들고 기존 티켓은 재사용한다."""
    order = dict(order)
    guild_id = int(order["guild_id"])
    buyer_id = int(order["buyer_id"])
    order_id = str(order["order_id"])
    guild = bot.get_guild(guild_id)
    if guild is None:
        LOGGER.warning("구매티켓 생성 실패: 서버를 찾을 수 없음 guild=%s order=%s", guild_id, order_id)
        return None

    async with bot.db_pool.acquire() as conn:
        saved = await conn.fetchrow(
            "SELECT status, purchase_ticket_channel_id FROM orders WHERE order_id=$1 AND guild_id=$2",
            order_id, guild_id,
        )
        if not saved or saved["status"] != "APPROVED":
            return None
        if saved["purchase_ticket_channel_id"]:
            existing = guild.get_channel(int(saved["purchase_ticket_channel_id"]))
            if isinstance(existing, discord.TextChannel):
                return existing

        settings = await conn.fetchrow(
            "SELECT ticket_cat_purchase FROM guild_settings WHERE guild_id=$1", guild_id,
        )
        user_info = await conn.fetchrow(
            "SELECT name, contact, address, cvs FROM user_info WHERE user_id=$1", buyer_id,
        )
        admin_rows = await conn.fetch(
            "SELECT role_id FROM guild_admin_roles WHERE guild_id=$1", guild_id,
        )

    member = guild.get_member(buyer_id)
    if member is None:
        try:
            member = await guild.fetch_member(buyer_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.exception("구매티켓 생성 실패: 구매자가 서버에 없음 user=%s order=%s", buyer_id, order_id)
            return None

    category_id = settings["ticket_cat_purchase"] if settings else None
    category = guild.get_channel(int(category_id)) if category_id else None
    if not isinstance(category, discord.CategoryChannel):
        category = None

    overwrites = dict(category.overwrites) if category else {}
    overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
    member_permissions = discord.PermissionOverwrite(
        view_channel=True,
        read_messages=True,
        read_message_history=True,
        send_messages=True,
        attach_files=True,
        embed_links=True,
    )
    overwrites[member] = member_permissions
    for row in admin_rows:
        role = guild.get_role(int(row["role_id"]))
        if role:
            overwrites[role] = member_permissions
    if guild.owner:
        overwrites[guild.owner] = member_permissions
    if guild.me:
        overwrites[guild.me] = member_permissions

    channel = await guild.create_text_channel(
        name=purchase_ticket_channel_name(member.display_name, order_id),
        category=category,
        overwrites=overwrites,
        reason=f"주문 {order_id} 결제 승인 자동 구매티켓",
    )
    try:
        async with bot.db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO tickets (channel_id, user_id, ticket_type)
                    VALUES ($1, $2, 'purchase')
                    ON CONFLICT (channel_id) DO UPDATE
                    SET user_id=EXCLUDED.user_id, ticket_type='purchase'
                    """,
                    channel.id, buyer_id,
                )
                await conn.execute(
                    "UPDATE orders SET purchase_ticket_channel_id=$3 WHERE order_id=$1 AND guild_id=$2",
                    order_id, guild_id, channel.id,
                )
    except Exception:
        try:
            await channel.delete(reason="구매티켓 DB 연결 실패 정리")
        except discord.HTTPException:
            pass
        raise

    info = dict(user_info) if user_info else {}
    address = order.get("shipping_address") or info.get("address") or ""
    cvs = order.get("shipping_cvs") or info.get("cvs") or ""
    shipping = {
        "method": order.get("shipping_method"),
        "name": order.get("shipping_name") or info.get("name"),
        "contact": order.get("shipping_contact") or info.get("contact"),
        "destination": cvs or address,
    }
    try:
        await bot.http.request(
            discord.http.Route("POST", f"/channels/{channel.id}/messages"),
            json=_ticket_payload(order, buyer_id, shipping),
        )
    except Exception:
        LOGGER.exception("구매티켓 안내 전송 실패: channel=%s order=%s", channel.id, order_id)
    return channel
