"""결제 승인 뒤 Discord 로그·역할·알림을 한곳에서 처리한다."""

from __future__ import annotations

import datetime
import logging

import discord

from loyalty_points import payment_summary
from order_editing import build_purchase_log_payload, sync_buyer_roles
from purchase_tickets import ensure_approved_purchase_ticket


LOGGER = logging.getLogger(__name__)
KST = datetime.timezone(datetime.timedelta(hours=9))


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
    settings = dict(settings_row) if settings_row else {}

    if guild:
        try:
            await ensure_approved_purchase_ticket(bot, order)
        except Exception:
            LOGGER.exception("자동승인 구매티켓 생성 실패: order=%s", order["order_id"])
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
        products = "\n".join(f"`{part.strip()}`" for part in str(order["product"]).split(",") if part.strip())
        content = (
            "## 📋 구매자 상세 배송 정보\n\n"
            f"`👤` **구매자:** <@{buyer_id}>\n"
            f"`🕒` **구매일시:** `{datetime.datetime.now(KST):%Y-%m-%d %H:%M:%S}`\n"
            f"`🧾` **주문번호:** `{order['order_id']}`\n"
            f"`👤` **성함:** `{shipping_name}`\n"
            f"`📞` **연락처:** `{shipping_contact}`\n"
            f"`🚚` **배송방식:** `{shipping_method}`\n"
            f"`🏠` **주소:** `{shipping_address}`\n"
            f"`🏪` **편의점:** `{shipping_cvs}`\n"
            f"`📦` **상품:**\n{products}\n\n"
            f"`💰` **금액:** `{order['amount']}`"
        )
        try:
            await _send_channel(bot, settings["buyer_info_channel_id"], _v2(content, users=[buyer_id]))
        except Exception:
            LOGGER.exception("자동승인 구매자 정보 전송 실패: order=%s", order["order_id"])

    if settings.get("log_channel_id"):
        try:
            response = await _send_channel(
                bot,
                settings["log_channel_id"],
                build_purchase_log_payload(order, settings.get("anonymous_role_id")),
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
        await _send_channel(bot, settings.get("approval_channel_id"), _v2(approval_text, users=[buyer_id]))
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
        "사용한 포인트와 상품 재고는 자동으로 복구되었습니다."
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

