"""결제 승인 뒤 Discord 로그·역할·알림을 한곳에서 처리한다."""

from __future__ import annotations

import datetime
import logging
import os

import discord

from loyalty_points import payment_summary
from order_editing import build_purchase_log_payload, format_order_items_for_discord, sync_buyer_roles


LOGGER = logging.getLogger(__name__)
KST = datetime.timezone(datetime.timedelta(hours=9))
STORE_ADMIN_USER_ID = int(os.getenv("WEB_ADMIN_USER_ID", "259758966043574272"))


def format_buyer_order_items(rows) -> str:
    """구매자정보 메시지에서 상품별 선택 옵션을 각각 독립된 줄로 표시한다."""
    return format_order_items_for_discord(rows)


def _v2(content: str, color: int = 0x32CD32, users: list[int] | None = None) -> dict:
    return {
        "flags": 1 << 15,
        "allowed_mentions": {"parse": [], "users": [str(user) for user in (users or [])]},
        "components": [{
            "type": 17,
            "accent_color": color,
            "components": [{"type": 10, "content": content}],
        }],
    }


async def _send_channel(bot, channel_id: int | None, payload: dict):
    if not channel_id:
        return None
    return await bot.http.request(
        discord.http.Route("POST", f"/channels/{int(channel_id)}/messages"),
        json=payload,
    )


async def _send_dm(bot, user_id: int, payload: dict):
    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    dm = await user.create_dm()
    return await _send_channel(bot, dm.id, payload)


async def notify_product_subadmins(bot, order: dict):
    """담당 상품이 포함된 승인 주문을 소유자와 해당 부관리자에게 알린다."""
    guild_id, buyer_id = int(order["guild_id"]), int(order["buyer_id"])
    async with bot.db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT s.user_id FROM web_order_items i
               JOIN web_subadmin_products s ON s.guild_id=$2 AND s.product_id=i.product_id
               WHERE i.order_id=$1""", order["order_id"], guild_id,
        )
        user_info = await conn.fetchrow("SELECT * FROM user_info WHERE user_id=$1", buyer_id)
    recipients = {int(row["user_id"]) for row in rows}
    owner_id = int(os.getenv("BOT_OWNER_ID", "0") or 0) or STORE_ADMIN_USER_ID
    if recipients: recipients.add(owner_id)
    if not recipients: return
    info = dict(user_info) if user_info else {}
    detail = (
        "## 🛒 담당 상품 주문 승인\n\n"
        f"**주문번호:** `{order['order_id']}`\n**구매자:** <@{buyer_id}> (`{buyer_id}`)\n"
        f"**성함:** `{order.get('shipping_name') or info.get('name') or '미등록'}`\n"
        f"**연락처:** `{order.get('shipping_contact') or info.get('contact') or '미등록'}`\n"
        f"**배송방식:** `{order.get('shipping_method') or '미등록'}`\n"
        f"**주소:** `{order.get('shipping_address') or info.get('address') or '미등록'}`\n"
        f"**편의점:** `{order.get('shipping_cvs') or info.get('cvs') or '없음'}`\n"
        f"**배송메모:** `{order.get('shipping_memo') or '없음'}`\n"
        f"**상품:** `{order['product']}`\n**결제금액:** `{order['amount']}`"
    )
    for recipient_id in recipients:
        try: await _send_dm(bot, recipient_id, _v2(detail, users=[buyer_id]))
        except (discord.Forbidden, discord.NotFound, discord.HTTPException): pass


async def finalize_approved_order(bot, order: dict, *, processor: str = "Pushbullet 자동승인"):
    """자동승인 주문에도 기존 구매로그, 누적액, 역할, 랭킹 동기화를 적용한다."""
    guild_id = int(order["guild_id"])
    buyer_id = int(order["buyer_id"])
    guild = bot.get_guild(guild_id)
    async with bot.db_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE orders
            SET fulfillment_status='PAYMENT_APPROVED', fulfillment_updated_at=CURRENT_TIMESTAMP
            WHERE order_id=$1 AND guild_id=$2
            """,
            order["order_id"], guild_id,
        )
        settings_row = await conn.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", guild_id)
        user_info = await conn.fetchrow("SELECT * FROM user_info WHERE user_id = $1", buyer_id)
        category_rows = await conn.fetch(
            """SELECT DISTINCT COALESCE(parent.name || ' › ' || category.name, category.name) category_name
               FROM web_order_items item
               JOIN web_products product ON product.id=item.product_id AND product.guild_id=$2
               LEFT JOIN web_categories category ON category.id=product.category_id
               LEFT JOIN web_categories parent ON parent.id=category.parent_id
               WHERE item.order_id=$1 AND category.name IS NOT NULL
               ORDER BY category_name""",
            order["order_id"], guild_id,
        )
        item_rows = await conn.fetch(
            """SELECT item.product_name,item.quantity,item.product_options_json,
                      COALESCE(product.option_groups_json, '[]') option_groups_json
               FROM web_order_items item
               LEFT JOIN web_products product ON product.id=item.product_id AND product.guild_id=$2
               WHERE item.order_id=$1
               ORDER BY item.product_name""",
            order["order_id"], guild_id,
        )
    settings = dict(settings_row) if settings_row else {}

    await notify_product_subadmins(bot, order)

    if guild:
        try:
            await sync_buyer_roles(bot, guild, buyer_id)
        except Exception:
            LOGGER.exception("자동승인 후 구매 역할 동기화 실패: order=%s", order["order_id"])
    refresh = getattr(bot, "refresh_purchase_leaderboard", None)
    if refresh:
        try:
            await refresh(guild_id)
        except Exception:
            LOGGER.exception("자동승인 후 구매랭킹 갱신 실패: order=%s", order["order_id"])

    if settings.get("buyer_info_channel_id"):
        info = dict(user_info) if user_info else {}
        shipping_name = order.get("shipping_name") or info.get("name") or "미등록"
        shipping_contact = order.get("shipping_contact") or info.get("contact") or "미등록"
        shipping_address = order.get("shipping_address") or info.get("address") or "미등록"
        shipping_cvs = order.get("shipping_cvs") or info.get("cvs") or "X"
        shipping_method = order.get("shipping_method") or "미등록"
        products = format_buyer_order_items(item_rows) or "\n".join(
            f"`{part.strip()}`" for part in str(order["product"]).split(",") if part.strip()
        )
        categories = ", ".join(str(row["category_name"]) for row in category_rows) or "미분류"
        content = (
            "## 📋 구매자 상세 배송 정보\n\n"
            f"<@{STORE_ADMIN_USER_ID}>\n\n"
            f"`👤` **구매자:** <@{buyer_id}>\n"
            f"`🕒` **구매일시:** `{datetime.datetime.now(KST):%Y-%m-%d %H:%M:%S}`\n"
            f"`🧾` **주문번호:** `{order['order_id']}`\n"
            f"`👤` **성함:** `{shipping_name}`\n"
            f"`📞` **연락처:** `{shipping_contact}`\n"
            f"`🚚` **배송방식:** `{shipping_method}`\n"
            f"`🏠` **주소:** `{shipping_address}`\n"
            f"`🏪` **편의점:** `{shipping_cvs}`\n"
            f"`📝` **배송메모:** `{order.get('shipping_memo') or '없음'}`\n"
            f"`🗂️` **상품 카테고리:** `{categories}`\n"
            f"`📦` **상품:**\n{products}\n\n"
            f"`💰` **금액:** `{order['amount']}`"
        )
        try:
            await _send_channel(bot, settings["buyer_info_channel_id"], _v2(content, users=[STORE_ADMIN_USER_ID, buyer_id]))
        except Exception:
            LOGGER.exception("자동승인 구매자 정보 전송 실패: order=%s", order["order_id"])

    if settings.get("log_channel_id"):
        try:
            purchase_log_order = dict(order)
            purchase_log_order["_formatted_items"] = format_buyer_order_items(item_rows)
            response = await _send_channel(
                bot,
                settings["log_channel_id"],
                build_purchase_log_payload(purchase_log_order, settings.get("anonymous_role_id")),
            )
            if response and response.get("id"):
                async with bot.db_pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE orders SET purchase_log_channel_id = $3, purchase_log_message_id = $4
                        WHERE order_id = $1 AND guild_id = $2
                        """,
                        order["order_id"], guild_id, int(settings["log_channel_id"]), int(response["id"]),
                    )
        except Exception:
            LOGGER.exception("자동승인 구매로그 전송 실패: order=%s", order["order_id"])

    approval_text = (
        "## ✅ 입금 자동확인 완료\n\n"
        f"`👤` **구매자**\n<@{buyer_id}>\n\n"
        f"`✍️` **입금자명**\n`{order.get('depositor_name') or '미입력'}`\n\n"
        f"`◾` **주문번호**\n`{order['order_id']}`\n\n"
        f"`💰` **금액**\n{payment_summary(order)}\n\n"
        f"`⚡` **처리방식**\n`{processor}`"
    )
    try:
        approval_payload = _v2(approval_text, users=[buyer_id])
        approval_channel_id = order.get("approval_channel_id") or settings.get("approval_channel_id")
        approval_message_id = order.get("approval_message_id")
        if approval_channel_id and approval_message_id:
            await bot.http.request(
                discord.http.Route("PATCH", f"/channels/{int(approval_channel_id)}/messages/{int(approval_message_id)}"),
                json=approval_payload,
            )
        else:
            await _send_channel(bot, approval_channel_id, approval_payload)
    except Exception:
        LOGGER.exception("자동승인 로그 전송 실패: order=%s", order["order_id"])

    try:
        await _send_dm(
            bot,
            buyer_id,
            _v2(
                "## ✅ 주문 승인완료\n\n"
                "입금이 자동으로 확인되어 주문이 승인되었습니다.\n\n"
                f"`◼️` **주문번호** : `{order['order_id']}`\n"
                f"`◼️` **상품** : `{order['product']}`\n"
                f"`◼️` **결제금액** : `{int(order.get('cash_amount') or 0):,}원`"
            ),
        )
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass


async def notify_expired_order(bot, order: dict):
    buyer_id = int(order["buyer_id"])
    guild_id = int(order["guild_id"])
    async with bot.db_pool.acquire() as conn:
        settings = await conn.fetchrow(
            "SELECT approval_channel_id FROM guild_settings WHERE guild_id = $1", guild_id
        )
    content = (
        "## ⏱️ 웹 주문 자동취소\n\n"
        f"`👤` **구매자**\n<@{buyer_id}>\n\n"
        f"`◾` **주문번호**\n`{order['order_id']}`\n\n"
        "신청 후 5분 안에 일치하는 입금이 확인되지 않아 주문을 취소했습니다.\n"
        "사용한 포인트는 자동으로 복구되었습니다."
    )
    try:
        await _send_channel(
            bot,
            settings.get("approval_channel_id") if settings else None,
            _v2(content, color=0xE74C3C, users=[buyer_id]),
        )
    except Exception:
        LOGGER.exception("만료 주문 로그 전송 실패: order=%s", order["order_id"])
    try:
        await _send_dm(
            bot,
            buyer_id,
            _v2(
                "## ❌ 웹 주문 자동취소\n\n"
                "주문 신청 후 5분 안에 입금이 확인되지 않아 자동 취소되었습니다.\n"
                f"`◼️` **주문번호** : `{order['order_id']}`\n\n"
                "사용한 포인트가 있다면 잔액으로 복구되었습니다.",
                color=0xE74C3C,
            ),
        )
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass

