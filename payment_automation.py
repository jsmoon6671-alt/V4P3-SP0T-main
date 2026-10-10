"""Pushbullet Android 알림을 이용한 웹 주문 자동 입금 확인.

Pushbullet의 notification mirroring은 저장형 push가 아니라 realtime stream의
``type=push`` / ``push.type=mirror`` 이벤트다. 이 모듈은 해당 스트림을 계속
수신하고, 입금자명과 현금 결제액이 모두 일치하는 대기 주문 하나만 승인한다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import quote

import aiohttp

from loyalty_points import PointsError, resolve_payment


LOGGER = logging.getLogger(__name__)
PAYMENT_TIMEOUT_SECONDS = 300
_INCOMING_WORDS = ("입금", "받았습니다", "받았어요", "들어왔", "보냈어요", "보냈습니다")
_OUTGOING_WORDS = ("출금", "결제 승인", "체크카드", "신용카드")
_NAME_TOKEN = r"[가-힣A-Za-z0-9][가-힣A-Za-z0-9·._-]{1,29}"


@dataclass(frozen=True)
class DepositNotice:
    event_id: str
    depositor_name: str
    amount: int
    title: str
    body: str
    application_name: str
    package_name: str


def normalize_depositor(value: str) -> str:
    """사람이 입력한 이름을 비교용 키로 정규화한다."""
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(ch for ch in value if ch.isalnum())


def _amount(value: str) -> int | None:
    try:
        result = int(value.replace(",", "").replace(" ", ""))
    except (AttributeError, ValueError):
        return None
    return result if result > 0 else None


def _event_id(push: dict) -> str:
    stable = {
        "source_device_iden": push.get("source_device_iden"),
        "package_name": push.get("package_name"),
        "notification_id": push.get("notification_id"),
        "notification_tag": push.get("notification_tag"),
        "created": push.get("created"),
    }
    # 일부 오래된 Android 클라이언트는 created를 보내지 않는다. 그런 경우에만
    # 내용 해시를 보조키로 사용하고, 일반적인 알림 업데이트는 한 번만 처리한다.
    if stable["created"] is None:
        stable["title"] = push.get("title")
        stable["body"] = push.get("body")
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _configured_match(text: str, pattern: str) -> tuple[str, int] | None:
    try:
        match = re.search(pattern, text, re.IGNORECASE)
    except re.error as exc:
        LOGGER.error("PUSHBULLET_DEPOSIT_REGEX가 올바르지 않습니다: %s", exc)
        return None
    if not match or "name" not in match.groupdict() or "amount" not in match.groupdict():
        return None
    amount = _amount(match.group("amount"))
    name = match.group("name").strip()
    return (name, amount) if name and amount else None


def _default_match(text: str) -> tuple[str, int] | None:
    if not any(word in text for word in _INCOMING_WORDS):
        return None
    if any(word in text for word in _OUTGOING_WORDS) and "입금" not in text:
        return None

    patterns = (
        rf"(?:입금자|보낸\s*분|보낸\s*사람|송금인|성명)\s*[:：]?\s*(?P<name>{_NAME_TOKEN}).*?(?P<amount>[0-9][0-9,]*)\s*원",
        r"(?P<name>[가-힣]{2,10}|[A-Za-z][A-Za-z0-9._-]{1,29})\s*님(?:이|가)?\s*(?P<amount>[0-9][0-9,]*)\s*원을?\s*(?:보냈|입금|송금)",
        rf"(?P<amount>[0-9][0-9,]*)\s*원\s*(?:입금|받음|도착)\s*[:：-]?\s*(?P<name>{_NAME_TOKEN})",
        rf"(?:입금|받음|도착)\s*[:：-]?\s*(?P<amount>[0-9][0-9,]*)\s*원\s*[:：-]?\s*(?P<name>{_NAME_TOKEN})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        name = match.group("name").strip()
        amount = _amount(match.group("amount"))
        if name and amount and "*" not in name:
            return name, amount
    return None


def parse_deposit_notice(push: dict, *, pattern: str | None = None) -> DepositNotice | None:
    """Pushbullet mirror payload에서 입금자명과 금액을 추출한다.

    은행별 문구가 다르면 ``PUSHBULLET_DEPOSIT_REGEX``에 ``name``과 ``amount``
    named group을 가진 정규식을 지정할 수 있다.
    """
    if push.get("type") != "mirror" or push.get("encrypted"):
        return None

    allowed_packages = {
        item.strip() for item in os.getenv("PUSHBULLET_ALLOWED_PACKAGES", "").split(",") if item.strip()
    }
    package_name = str(push.get("package_name") or "")
    if allowed_packages and package_name not in allowed_packages:
        return None

    title = str(push.get("title") or "").strip()
    body = str(push.get("body") or "").strip()
    text = " ".join((title, body)).strip()
    if not text:
        return None

    custom_pattern = pattern if pattern is not None else os.getenv("PUSHBULLET_DEPOSIT_REGEX", "").strip()
    parsed = _configured_match(text, custom_pattern) if custom_pattern else _default_match(text)
    if not parsed:
        return None
    name, amount = parsed
    if not normalize_depositor(name):
        return None
    return DepositNotice(
        event_id=_event_id(push),
        depositor_name=name,
        amount=amount,
        title=title,
        body=body,
        application_name=str(push.get("application_name") or ""),
        package_name=package_name,
    )


async def initialize_payment_automation_schema(conn):
    await conn.execute(
        """
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'DISCORD';
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_deadline TIMESTAMPTZ;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS depositor_key TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS auto_payment_event_id TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS stock_restored BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS cancel_reason TEXT;

        CREATE TABLE IF NOT EXISTS web_payment_events (
            event_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            application_name TEXT NOT NULL DEFAULT '',
            package_name TEXT NOT NULL DEFAULT '',
            depositor_name TEXT,
            depositor_key TEXT,
            amount BIGINT,
            status TEXT NOT NULL,
            matched_order_id TEXT,
            received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            processed_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_orders_web_payment_match
            ON orders (status, source, depositor_key, cash_amount, payment_deadline);
        CREATE INDEX IF NOT EXISTS idx_orders_web_payment_expiry
            ON orders (payment_deadline)
            WHERE status = 'PENDING' AND source = 'WEB';
        """
    )


async def arm_web_payment(conn, order_id: str, guild_id: int, *, timeout_seconds: int = PAYMENT_TIMEOUT_SECONDS):
    """결제 요청 시점부터 정확히 timeout_seconds 후 만료되도록 설정한다."""
    row = await conn.fetchrow(
        """
        UPDATE orders
        SET source = 'WEB',
            depositor_key = $3,
            payment_deadline = CURRENT_TIMESTAMP + ($4::integer * INTERVAL '1 second'),
            auto_payment_event_id = NULL,
            stock_restored = FALSE,
            cancel_reason = NULL
        WHERE order_id = $1 AND guild_id = $2
          AND status = 'PENDING' AND payment_requested = TRUE
        RETURNING *
        """,
        order_id,
        guild_id,
        normalize_depositor(await conn.fetchval(
            "SELECT depositor_name FROM orders WHERE order_id = $1 AND guild_id = $2", order_id, guild_id
        ) or ""),
        timeout_seconds,
    )
    if row is None:
        raise PointsError("결제 대기 상태로 전환할 수 없는 주문입니다.")
    return dict(row)


async def _record_and_claim(conn, notice: DepositNotice) -> dict | None:
    async with conn.transaction():
        inserted = await conn.fetchval(
            """
            INSERT INTO web_payment_events (
                event_id, title, body, application_name, package_name,
                depositor_name, depositor_key, amount, status
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'RECEIVED')
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id
            """,
            notice.event_id,
            "",
            "",
            notice.application_name,
            notice.package_name,
            notice.depositor_name,
            normalize_depositor(notice.depositor_name),
            notice.amount,
        )
        if inserted is None:
            return None

        matches = await conn.fetch(
            """
            SELECT * FROM orders
            WHERE status = 'PENDING'
              AND source = 'WEB'
              AND payment_requested = TRUE
              AND auto_payment_event_id IS NULL
              AND payment_deadline > CURRENT_TIMESTAMP
              AND depositor_key = $1
              AND cash_amount = $2
            ORDER BY payment_deadline ASC, created_at ASC
            LIMIT 2
            FOR UPDATE SKIP LOCKED
            """,
            normalize_depositor(notice.depositor_name),
            notice.amount,
        )
        if len(matches) != 1:
            status = "AMBIGUOUS" if len(matches) > 1 else "UNMATCHED"
            await conn.execute(
                "UPDATE web_payment_events SET status = $2, processed_at = CURRENT_TIMESTAMP WHERE event_id = $1",
                notice.event_id,
                status,
            )
            return None

        order = dict(matches[0])
        await conn.execute(
            """
            UPDATE orders SET auto_payment_event_id = $2
            WHERE order_id = $1 AND auto_payment_event_id IS NULL
            """,
            order["order_id"],
            notice.event_id,
        )
        await conn.execute(
            """
            UPDATE web_payment_events
            SET status = 'MATCHED', matched_order_id = $2, processed_at = CURRENT_TIMESTAMP
            WHERE event_id = $1
            """,
            notice.event_id,
            order["order_id"],
        )
        return order


async def _expire_order(conn, order_id: str) -> dict | None:
    async with conn.transaction():
        row = await conn.fetchrow(
            """
            SELECT * FROM orders
            WHERE order_id = $1 FOR UPDATE
            """,
            order_id,
        )
        if row is None:
            return None
        order = dict(row)
        deadline = order.get("payment_deadline")
        now = datetime.now(timezone.utc)
        if (
            order.get("status") != "PENDING"
            or order.get("source") != "WEB"
            or order.get("auto_payment_event_id")
            or deadline is None
            or deadline > now
        ):
            return None

        points = int(order.get("points_used") or 0)
        if points and not order.get("points_refunded"):
            await conn.execute(
                """
                INSERT INTO point_balances (guild_id, user_id, balance)
                VALUES ($1, $2, 0)
                ON CONFLICT (guild_id, user_id) DO NOTHING
                """,
                order["guild_id"],
                order["buyer_id"],
            )
            await conn.execute(
                """
                UPDATE point_balances SET balance = balance + $3
                WHERE guild_id = $1 AND user_id = $2
                """,
                order["guild_id"],
                order["buyer_id"],
                points,
            )

        if not order.get("stock_restored"):
            await conn.execute(
                """
                UPDATE web_products p
                SET stock = p.stock + i.quantity
                FROM web_order_items i
                WHERE i.order_id = $1 AND i.product_id = p.id
                """,
                order_id,
            )

        updated = await conn.fetchrow(
            """
            UPDATE orders
            SET status = 'CANCELLED',
                payment_requested = FALSE,
                points_refunded = CASE WHEN points_used > 0 THEN TRUE ELSE points_refunded END,
                stock_restored = TRUE,
                cancel_reason = '5분 이내 입금 미확인',
                processed_at = CURRENT_TIMESTAMP
            WHERE order_id = $1
            RETURNING *
            """,
            order_id,
        )
        return dict(updated)


class PushbulletPaymentService:
    def __init__(
        self,
        bot,
        on_approved: Callable[[dict, DepositNotice], Awaitable[None]],
        on_expired: Callable[[dict], Awaitable[None]],
    ):
        self.bot = bot
        self.on_approved = on_approved
        self.on_expired = on_expired
        self.token = os.getenv("PUSHBULLET_ACCESS_TOKEN", "").strip()
        self._stream_task: asyncio.Task | None = None
        self._expiry_task: asyncio.Task | None = None
        self._closed = False

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def start(self):
        if self._expiry_task is None or self._expiry_task.done():
            self._expiry_task = asyncio.create_task(self._expiry_loop(), name="web-payment-expiry")
        if self.token and (self._stream_task is None or self._stream_task.done()):
            self._stream_task = asyncio.create_task(self._stream_loop(), name="pushbullet-payments")
            LOGGER.info("Pushbullet 자동 결제 승인을 시작했습니다.")
        elif not self.token:
            LOGGER.warning("PUSHBULLET_ACCESS_TOKEN이 없어 자동 입금 승인이 비활성화되었습니다.")

    async def close(self):
        self._closed = True
        tasks = [task for task in (self._stream_task, self._expiry_task) if task]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    async def process_push(self, push: dict) -> dict | None:
        notice = parse_deposit_notice(push)
        if notice is None:
            return None
        async with self.bot.db_pool.acquire() as conn:
            claimed = await _record_and_claim(conn, notice)
        if claimed is None:
            return None
        try:
            async with self.bot.db_pool.acquire() as conn:
                approved = await resolve_payment(
                    conn, claimed["order_id"], int(claimed["guild_id"]), True
                )
                await conn.execute(
                    "UPDATE web_payment_events SET status = 'APPROVED' WHERE event_id = $1",
                    notice.event_id,
                )
        except Exception:
            LOGGER.exception("Pushbullet 자동 승인 처리 실패: order=%s", claimed["order_id"])
            async with self.bot.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE web_payment_events SET status = 'ERROR' WHERE event_id = $1",
                    notice.event_id,
                )
                await conn.execute(
                    """
                    UPDATE orders SET auto_payment_event_id = NULL
                    WHERE order_id = $1 AND status = 'PENDING'
                    """,
                    claimed["order_id"],
                )
            raise
        try:
            await self.on_approved(approved, notice)
        except Exception:
            # 결제 DB 승인은 완료된 상태이므로 Discord 부가 알림 실패로 되돌리지 않는다.
            LOGGER.exception("자동승인 후 Discord 처리 실패: order=%s", claimed["order_id"])
        return approved

    async def _stream_loop(self):
        delay = 2
        url = f"wss://stream.pushbullet.com/websocket/{quote(self.token, safe='')}"
        while not self._closed:
            try:
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=75)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(url, heartbeat=30) as websocket:
                        delay = 2
                        async for message in websocket:
                            if message.type == aiohttp.WSMsgType.TEXT:
                                payload = json.loads(message.data)
                                if payload.get("type") == "push":
                                    push = payload.get("push") or {}
                                    if push.get("encrypted"):
                                        LOGGER.warning("암호화된 Pushbullet 알림은 입금 확인에 사용할 수 없습니다.")
                                    elif push.get("type") == "mirror":
                                        await self.process_push(push)
                            elif message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("Pushbullet 연결이 끊겼습니다. %s초 후 재연결합니다: %s", delay, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _expiry_loop(self):
        while not self._closed:
            try:
                async with self.bot.db_pool.acquire() as conn:
                    ids = await conn.fetch(
                        """
                        SELECT order_id FROM orders
                        WHERE status = 'PENDING' AND source = 'WEB'
                          AND payment_deadline <= CURRENT_TIMESTAMP
                          AND auto_payment_event_id IS NULL
                        ORDER BY payment_deadline ASC LIMIT 100
                        """
                    )
                for row in ids:
                    async with self.bot.db_pool.acquire() as conn:
                        expired = await _expire_order(conn, row["order_id"])
                    if expired:
                        try:
                            await self.on_expired(expired)
                        except Exception:
                            LOGGER.exception("만료 주문 알림 전송 실패: order=%s", expired["order_id"])
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("웹 주문 만료 확인 중 오류가 발생했습니다.")
            await asyncio.sleep(2)

