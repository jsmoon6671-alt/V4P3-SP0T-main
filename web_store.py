"""Discord 로그인 기반 V4P3 SP0T 웹 스토어와 봇 연동 서버."""

from __future__ import annotations

import base64
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
import discord
from aiohttp import web

from delivery_tracking import (
    DeliveryTrackingError,
    TRACKING_PANEL_CARRIERS,
    normalize_waybill,
    track_shipment,
)
from loyalty_points import PointsError, adjust_points, award_review_points, get_balance, maximum_points, parse_amount, request_payment, resolve_payment
from order_editing import sync_buyer_roles
from order_fulfillment import finalize_approved_order, notify_expired_order
from payment_automation import (
    DepositNotice,
    PushbulletPaymentService,
    arm_web_payment,
    initialize_payment_automation_schema,
)
from product_scraper import (
    import_scraped_products,
    scrape_device_stores,
    scrape_products_from_url,
    sync_scraped_products,
)
from store_lookup import StoreLookupError, search_stores
from review_watermark import ReviewImageError, watermark_review_image


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "web" / "static"
COOKIE = "v4p3_session"
ADMIN_USER_ID = 259758966043574272
BASE_SHIPPING_FEE = 3_000
DISCOUNTED_SHIPPING_FEE = 1_500
DISCOUNTED_SHIPPING_THRESHOLD = 35_000
FREE_SHIPPING_THRESHOLD = 50_000
LOGGER = logging.getLogger(__name__)
REVIEW_SYNC_INTERVAL = 300
OPTION_SURCHARGE_PATTERN = re.compile(r"\(\+\s*([0-9][0-9,]*)\s*(?:원)?\)")
COUPON_TIER_THRESHOLDS = {
    "NONE": 0,
    "VIP": 50_000,
    "VVIP": 100_000,
    "SVIP": 300_000,
    "ROYAL": 500_000,
    "VAPE_MASTER": 800_000,
    "VAPE_GOD": 1_000_000,
}
COUPON_TIER_LABELS = {
    "NONE": "등급 제한 없음", "VIP": "VIP", "VVIP": "VVIP", "SVIP": "SVIP",
    "ROYAL": "ROYAL", "VAPE_MASTER": "VAPE MASTER", "VAPE_GOD": "VAPE GOD",
}
DEFAULT_TERMS_OF_SERVICE = """V4P3 SP0T 이용약관

1. 모든 상품 구매는 사이트 및 지정된 문의 채널에서 진행합니다.
2. 배송을 위해 실명, 전화번호와 배송지를 정확히 입력해야 합니다.
3. 모든 상품은 선불이며 입금 확인 후 구매가 진행됩니다.
4. 단순 변심으로 인한 환불은 불가능합니다.
5. 상품 품절 등 판매자 사유로 진행할 수 없는 주문은 환불할 수 있습니다.
6. 택배사 또는 공급사의 사정과 잘못 입력한 배송 정보로 발생한 문제는 보상이 제한될 수 있습니다.
7. 모든 이용자는 관리자 안내와 커뮤니티 운영 규칙을 따라야 합니다."""
DEFAULT_REFUND_POLICY = """환불약관

1. 단순 변심으로 인한 환불은 불가능합니다.
2. 상품 품절 등 판매자 사유로 주문을 진행할 수 없는 경우 환불합니다.
3. 이미 구매 또는 발주가 진행된 주문은 환불이 제한될 수 있습니다.
4. 환불계좌 정보가 정확하지 않아 발생하는 지연은 구매자에게 책임이 있습니다.
5. 환불이 결정되면 등록된 은행, 계좌번호와 예금주 정보를 기준으로 처리합니다."""


def calculate_shipping_fee(subtotal: int) -> int:
    """상품 금액에 따른 배송비를 계산한다."""
    subtotal = max(0, int(subtotal or 0))
    if subtotal == 0 or subtotal >= FREE_SHIPPING_THRESHOLD:
        return 0
    if subtotal >= DISCOUNTED_SHIPPING_THRESHOLD:
        return DISCOUNTED_SHIPPING_FEE
    return BASE_SHIPPING_FEE


def qualifies_for_product_free_shipping(items) -> bool:
    """한 종류의 상품만 결제하며 설정 수량을 채웠을 때만 무료배송을 적용한다."""
    rows = list(items)
    if len(rows) != 1:
        return False
    row = rows[0]
    threshold = int(row.get("free_shipping_quantity") or 0)
    return threshold > 0 and int(row.get("quantity") or 0) >= threshold


def option_surcharge(option: str) -> int:
    """옵션명의 (+2000), (+2,000원) 표기를 개당 추가 금액으로 해석한다."""
    return sum(int(value.replace(",", "")) for value in OPTION_SURCHARGE_PATTERN.findall(str(option or "")))


def parse_product_options(raw_options) -> list[str]:
    """줄바꿈이나 괄호 밖 쉼표로 옵션을 나누고 입력 순서대로 중복을 제거한다."""
    if isinstance(raw_options, list):
        values = raw_options
    elif isinstance(raw_options, str):
        values, buffer, depth = [], [], 0
        for char in raw_options.replace("\r\n", "\n").replace("\r", "\n"):
            if char == "(":
                depth += 1
            elif char == ")" and depth:
                depth -= 1
            if char == "\n" or (char == "," and depth == 0):
                values.append("".join(buffer))
                buffer = []
            else:
                buffer.append(char)
        values.append("".join(buffer))
    else:
        raise StoreError("옵션 목록을 확인해 주세요.")
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def parse_json_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def normalize_option_groups(raw_groups, legacy_label="없음", legacy_options=None) -> list[dict]:
    """상품 선택 항목을 최대 세 그룹으로 검증하고 정규화한다."""
    groups = parse_json_list(raw_groups)
    if not groups and legacy_label != "없음" and legacy_options:
        groups = [{"label": legacy_label, "options": legacy_options}]
    if len(groups) > 3:
        raise StoreError("선택 항목은 최대 3개까지만 만들 수 있습니다.")
    normalized = []
    unused_seen = False
    for group in groups:
        if not isinstance(group, dict):
            raise StoreError("선택 항목 형식을 확인해 주세요.")
        label = str(group.get("label", "없음")).strip()
        if label == "없음":
            unused_seen = True
            continue
        if unused_seen:
            raise StoreError("선택 항목은 1번부터 순서대로 만들어 주세요.")
        group_type = "text" if str(group.get("type", "choice")).strip().lower() == "text" else "choice"
        if not label:
            raise StoreError("질문 또는 옵션명을 작성해 주세요.")
        max_label_length = 100 if group_type == "text" else 30
        if len(label) > max_label_length or any(ord(character) < 32 for character in label):
            raise StoreError(f"{'질문' if group_type == 'text' else '옵션명'}은 줄바꿈 없이 {max_label_length}자 이하로 입력해 주세요.")
        if group_type == "text":
            normalized.append({"type": "text", "label": label, "options": []})
            continue
        options = parse_product_options(group.get("options", []))
        if not options:
            raise StoreError(f"{label} 항목을 한 개 이상 입력해 주세요.")
        if len(options) > 50:
            raise StoreError(f"{label} 항목은 최대 50개까지 입력할 수 있습니다.")
        normalized.append({"label": label, "options": options})
    return normalized


def normalize_selected_options(option_groups: list[dict], raw_selected) -> list[str]:
    selected = [str(value).strip() for value in parse_json_list(raw_selected)]
    if len(selected) != len(option_groups):
        raise StoreError("상품의 모든 선택 항목을 골라 주세요.")
    for index, group in enumerate(option_groups):
        if group.get("type") == "text":
            if not selected[index]:
                raise StoreError(f"{group['label']} 질문에 답변해 주세요.")
            if len(selected[index]) > 500 or any(ord(character) < 32 and character not in "\t\n\r" for character in selected[index]):
                raise StoreError(f"{group['label']} 답변은 500자 이하로 작성해 주세요.")
        elif selected[index] not in group["options"]:
            raise StoreError(f"상품의 {group['label']} 항목을 다시 선택해 주세요.")
    return selected


def selected_options_surcharge(selected_options) -> int:
    return sum(option_surcharge(value) for value in parse_json_list(selected_options))


def product_selection_surcharge(option_groups: list[dict], selected_options) -> int:
    """구매자 직접입력 답변은 추가금 문구로 해석하지 않는다."""
    selected = parse_json_list(selected_options)
    return sum(
        option_surcharge(value)
        for group, value in zip(option_groups, selected)
        if group.get("type") != "text"
    )


class StoreError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


async def initialize_web_store_schema(conn):
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS web_categories (
            id BIGSERIAL PRIMARY KEY,
            guild_id BIGINT NOT NULL,
            name TEXT NOT NULL,
            parent_id BIGINT REFERENCES web_categories(id) ON DELETE SET NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (guild_id, name)
        );
        CREATE TABLE IF NOT EXISTS web_products (
            id BIGSERIAL PRIMARY KEY,
            guild_id BIGINT NOT NULL,
            category_id BIGINT REFERENCES web_categories(id) ON DELETE SET NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            price BIGINT NOT NULL CHECK (price >= 0),
            stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
            image_url TEXT NOT NULL DEFAULT '',
            option_label TEXT NOT NULL DEFAULT '색상',
            options TEXT[] NOT NULL DEFAULT '{}',
            option_groups_json TEXT NOT NULL DEFAULT '[]',
            free_shipping_quantity INTEGER NOT NULL DEFAULT 0 CHECK (free_shipping_quantity BETWEEN 0 AND 99),
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS web_cart_items (
            guild_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            product_id BIGINT NOT NULL REFERENCES web_products(id) ON DELETE CASCADE,
            quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 99),
            selected_option TEXT NOT NULL DEFAULT '',
            selected_options_json TEXT NOT NULL DEFAULT '[]',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, user_id, product_id)
        );
        CREATE TABLE IF NOT EXISTS web_order_items (
            order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
            product_id BIGINT REFERENCES web_products(id) ON DELETE SET NULL,
            product_name TEXT NOT NULL,
            unit_price BIGINT NOT NULL CHECK (unit_price >= 0),
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            product_option TEXT NOT NULL DEFAULT '',
            product_options_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (order_id, product_id)
        );
        CREATE TABLE IF NOT EXISTS web_reviews (
            guild_id BIGINT NOT NULL,
            discord_message_id BIGINT NOT NULL,
            user_id BIGINT,
            rating SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
            content TEXT NOT NULL,
            image_url TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, discord_message_id)
        );
        CREATE TABLE IF NOT EXISTS web_review_images (
            guild_id BIGINT NOT NULL,
            review_id BIGINT NOT NULL,
            image_index SMALLINT NOT NULL,
            image_data BYTEA NOT NULL,
            PRIMARY KEY (guild_id, review_id, image_index)
        );
        CREATE TABLE IF NOT EXISTS web_product_sources (
            guild_id BIGINT NOT NULL,
            source_site TEXT NOT NULL,
            source_key TEXT NOT NULL,
            product_id BIGINT NOT NULL REFERENCES web_products(id) ON DELETE CASCADE,
            source_url TEXT NOT NULL DEFAULT '',
            source_price BIGINT NOT NULL DEFAULT 0,
            price_locked BOOLEAN NOT NULL DEFAULT FALSE,
            disabled_by_admin BOOLEAN NOT NULL DEFAULT FALSE,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, source_site, source_key)
        );
        CREATE TABLE IF NOT EXISTS web_product_images (
            product_id BIGINT PRIMARY KEY REFERENCES web_products(id) ON DELETE CASCADE,
            guild_id BIGINT NOT NULL,
            mime_type TEXT NOT NULL DEFAULT 'image/webp',
            content_hash TEXT NOT NULL,
            image_data BYTEA NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS web_blacklist (
            guild_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            reason TEXT NOT NULL,
            added_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, user_id)
        );
        ALTER TABLE web_blacklist ADD COLUMN IF NOT EXISTS blacklist_type TEXT NOT NULL DEFAULT 'PERMANENT';
        ALTER TABLE web_blacklist ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
        CREATE TABLE IF NOT EXISTS web_settings (
            guild_id BIGINT NOT NULL, setting_key TEXT NOT NULL, setting_value TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, setting_key)
        );
        CREATE TABLE IF NOT EXISTS web_notices (
            guild_id BIGINT NOT NULL, notice_type TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
            is_active BOOLEAN NOT NULL DEFAULT FALSE, image_mime TEXT, image_data BYTEA,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, notice_type)
        );
        ALTER TABLE web_notices ADD COLUMN IF NOT EXISTS image_mime TEXT;
        ALTER TABLE web_notices ADD COLUMN IF NOT EXISTS image_data BYTEA;
        CREATE TABLE IF NOT EXISTS web_coupons (
            id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, code TEXT NOT NULL,
            user_id BIGINT, discount_amount BIGINT NOT NULL CHECK (discount_amount > 0),
            expires_at TIMESTAMPTZ, used_order_id TEXT, min_order_amount BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (guild_id, code)
        );
        CREATE TABLE IF NOT EXISTS web_coupon_events (
            guild_id BIGINT NOT NULL, event_key TEXT NOT NULL, title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', discount_amount BIGINT NOT NULL CHECK (discount_amount > 0),
            valid_days INTEGER NOT NULL DEFAULT 30 CHECK (valid_days BETWEEN 1 AND 365),
            minimum_tier TEXT NOT NULL DEFAULT 'NONE', purchaser_requirement TEXT NOT NULL DEFAULT 'ALL',
            max_claims INTEGER NOT NULL DEFAULT 0, min_order_amount BIGINT NOT NULL DEFAULT 0,
            image_mime TEXT NOT NULL, image_data BYTEA NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, event_key)
        );
        CREATE TABLE IF NOT EXISTS web_coupon_claims (
            guild_id BIGINT NOT NULL, event_key TEXT NOT NULL, user_id BIGINT NOT NULL,
            coupon_id BIGINT NOT NULL REFERENCES web_coupons(id) ON DELETE CASCADE,
            claimed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, event_key, user_id)
        );
        ALTER TABLE web_coupons ADD COLUMN IF NOT EXISTS min_order_amount BIGINT NOT NULL DEFAULT 0;
        ALTER TABLE web_coupon_events ADD COLUMN IF NOT EXISTS minimum_tier TEXT NOT NULL DEFAULT 'NONE';
        ALTER TABLE web_coupon_events ADD COLUMN IF NOT EXISTS purchaser_requirement TEXT NOT NULL DEFAULT 'ALL';
        ALTER TABLE web_coupon_events ADD COLUMN IF NOT EXISTS max_claims INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE web_coupon_events ADD COLUMN IF NOT EXISTS min_order_amount BIGINT NOT NULL DEFAULT 0;
        CREATE TABLE IF NOT EXISTS web_age_confirmations (
            guild_id BIGINT NOT NULL,
            ip_hash TEXT NOT NULL,
            confirmed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, ip_hash)
        );
        CREATE TABLE IF NOT EXISTS web_inquiries (
            id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
            inquiry_type TEXT NOT NULL DEFAULT 'general', content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN', answer TEXT NOT NULL DEFAULT '',
            answered_by BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            answered_at TIMESTAMPTZ
        );
        CREATE TABLE IF NOT EXISTS web_chat_sessions (
            id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
            thread_id BIGINT UNIQUE, status TEXT NOT NULL DEFAULT 'OPEN', assigned_to BIGINT,
            subject TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, closed_at TIMESTAMPTZ
        );
        CREATE TABLE IF NOT EXISTS web_chat_messages (
            id BIGSERIAL PRIMARY KEY, session_id BIGINT NOT NULL REFERENCES web_chat_sessions(id) ON DELETE CASCADE,
            sender_type TEXT NOT NULL, sender_id BIGINT, sender_name TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '', image_mime TEXT, image_data BYTEA,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS web_direct_dm_sessions (
            guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS web_direct_dm_messages (
            id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
            sender_type TEXT NOT NULL, sender_name TEXT NOT NULL DEFAULT '', content TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS web_subadmins (
            guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, added_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS web_subadmin_products (
            guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
            product_id BIGINT NOT NULL REFERENCES web_products(id) ON DELETE CASCADE,
            PRIMARY KEY (guild_id, user_id, product_id),
            FOREIGN KEY (guild_id, user_id) REFERENCES web_subadmins(guild_id, user_id) ON DELETE CASCADE
        );
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS coupon_code TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS coupon_discount BIGINT NOT NULL DEFAULT 0;
        ALTER TABLE web_categories ADD COLUMN IF NOT EXISTS guild_id BIGINT NOT NULL DEFAULT 0;
        ALTER TABLE web_categories ADD COLUMN IF NOT EXISTS name TEXT NOT NULL DEFAULT '';
        ALTER TABLE web_categories ADD COLUMN IF NOT EXISTS parent_id BIGINT REFERENCES web_categories(id) ON DELETE SET NULL;
        ALTER TABLE web_categories ADD COLUMN IF NOT EXISTS sort_order INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE web_categories ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
        ALTER TABLE web_products ADD COLUMN IF NOT EXISTS guild_id BIGINT NOT NULL DEFAULT 0;
        ALTER TABLE web_products ADD COLUMN IF NOT EXISTS category_id BIGINT;
        ALTER TABLE web_products ADD COLUMN IF NOT EXISTS name TEXT NOT NULL DEFAULT '';
        ALTER TABLE web_products ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';
        ALTER TABLE web_products ADD COLUMN IF NOT EXISTS price BIGINT NOT NULL DEFAULT 0;
        ALTER TABLE web_products ADD COLUMN IF NOT EXISTS stock INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE web_products ADD COLUMN IF NOT EXISTS image_url TEXT NOT NULL DEFAULT '';
        ALTER TABLE web_products ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
        ALTER TABLE web_products ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
        ALTER TABLE web_products ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
        ALTER TABLE web_products ADD COLUMN IF NOT EXISTS option_label TEXT NOT NULL DEFAULT '색상';
        ALTER TABLE web_products ADD COLUMN IF NOT EXISTS options TEXT[] NOT NULL DEFAULT '{}';
        ALTER TABLE web_products ADD COLUMN IF NOT EXISTS option_groups_json TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE web_products ADD COLUMN IF NOT EXISTS free_shipping_quantity INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE web_cart_items ADD COLUMN IF NOT EXISTS selected_option TEXT NOT NULL DEFAULT '';
        ALTER TABLE web_cart_items ADD COLUMN IF NOT EXISTS selected_options_json TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE web_order_items ADD COLUMN IF NOT EXISTS product_option TEXT NOT NULL DEFAULT '';
        ALTER TABLE web_order_items ADD COLUMN IF NOT EXISTS product_options_json TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE web_product_sources ADD COLUMN IF NOT EXISTS disabled_by_admin BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE web_product_sources ADD COLUMN IF NOT EXISTS price_locked BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE web_reviews ADD COLUMN IF NOT EXISTS watermark_version SMALLINT NOT NULL DEFAULT 0;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_name TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_contact TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_address TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_cvs TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_method TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_fee BIGINT NOT NULL DEFAULT 0;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_memo TEXT NOT NULL DEFAULT '';
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS waybill_number TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS carrier_id TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS fulfillment_status TEXT NOT NULL DEFAULT 'PAYMENT_APPROVED';
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS fulfillment_updated_at TIMESTAMPTZ;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS last_tracking_event TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS last_tracking_notified_at TIMESTAMPTZ;
        ALTER TABLE user_info ADD COLUMN IF NOT EXISTS cvs_brand TEXT NOT NULL DEFAULT '';
        ALTER TABLE user_info ADD COLUMN IF NOT EXISTS cvs_address TEXT NOT NULL DEFAULT '';
        ALTER TABLE user_info ADD COLUMN IF NOT EXISTS refund_account TEXT NOT NULL DEFAULT '';
        ALTER TABLE user_info ADD COLUMN IF NOT EXISTS refund_bank TEXT NOT NULL DEFAULT '';
        ALTER TABLE user_info ADD COLUMN IF NOT EXISTS refund_account_number TEXT NOT NULL DEFAULT '';
        ALTER TABLE user_info ADD COLUMN IF NOT EXISTS refund_account_holder TEXT NOT NULL DEFAULT '';
        ALTER TABLE user_info ADD COLUMN IF NOT EXISTS refund_policy_version TEXT NOT NULL DEFAULT '';
        ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS inquiry_alert_channel_id BIGINT;
        ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS live_chat_channel_id BIGINT;
        ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS live_chat_start TEXT NOT NULL DEFAULT '10:00';
        ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS live_chat_end TEXT NOT NULL DEFAULT '22:00';
        ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS purchase_verified_role_id BIGINT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS approval_channel_id BIGINT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS approval_message_id BIGINT;

        UPDATE web_products
        SET option_groups_json = json_build_array(
            json_build_object('label', option_label, 'options', options)
        )::text
        WHERE option_groups_json = '[]' AND cardinality(options) > 0;

        UPDATE web_cart_items
        SET selected_options_json = json_build_array(selected_option)::text
        WHERE selected_options_json = '[]' AND selected_option <> '';

        UPDATE web_order_items
        SET product_options_json = json_build_array(product_option)::text
        WHERE product_options_json = '[]' AND product_option <> '';
        """
    )
    await initialize_payment_automation_schema(conn)


async def save_web_review(
    conn, guild_id: int, message_id: int, user_id: int | None,
    rating: int, content: str, image_url: str = "", created_at=None,
):
    await conn.execute(
        """
        INSERT INTO web_reviews
            (guild_id, discord_message_id, user_id, rating, content, image_url, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, COALESCE($7::timestamptz, CURRENT_TIMESTAMP))
        ON CONFLICT (guild_id, discord_message_id) DO UPDATE SET
            user_id=EXCLUDED.user_id, rating=EXCLUDED.rating,
            content=EXCLUDED.content,
            image_url=CASE
                WHEN web_reviews.image_url LIKE '/api/reviews/%' THEN web_reviews.image_url
                WHEN BTRIM(EXCLUDED.image_url) <> '' THEN EXCLUDED.image_url
                ELSE web_reviews.image_url
            END
        """,
        guild_id, message_id, user_id, rating, content.strip(), image_url.strip(), created_at,
    )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class StoreServer:
    def __init__(self, bot):
        self.bot = bot
        self.secret = os.getenv("WEB_SESSION_SECRET", "").encode()
        self.guild_id = int(os.getenv("WEB_GUILD_ID", "0") or 0)
        self.client_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
        self.redirect_uri = os.getenv("DISCORD_REDIRECT_URI", "").strip()
        self.public_url = os.getenv("WEB_PUBLIC_URL", "").rstrip("/")
        self._last_review_sync = 0.0
        self._product_sync_lock = asyncio.Lock()
        self._review_watermark_lock = asyncio.Lock()
        self.runner: web.AppRunner | None = None
        self.payment_service = PushbulletPaymentService(
            bot,
            self._payment_approved,
            self._payment_expired,
        )
        self.app = web.Application(
            client_max_size=32 * 1024 ** 2,
            middlewares=[self._errors, self._no_cache, self._session],
        )
        self._routes()

    @web.middleware
    async def _errors(self, request, handler):
        try:
            return await handler(request)
        except StoreError as exc:
            return web.json_response({"error": str(exc)}, status=exc.status)
        except PointsError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except web.HTTPException:
            raise
        except Exception as exc:
            error_id = secrets.token_hex(4).upper()
            LOGGER.exception(
                "웹 스토어 요청 처리 실패: error_id=%s method=%s path=%s",
                error_id, request.method, request.path,
            )
            if request.path.startswith("/api/admin/"):
                detail = str(exc).strip().replace("\n", " ")[:240]
                message = f"관리자 기능 오류: {type(exc).__name__}"
                if detail:
                    message += f" · {detail}"
                message += f" (오류코드: {error_id})"
            else:
                message = f"서버 처리 중 오류가 발생했습니다. 오류코드: {error_id}"
            return web.json_response({"error": message}, status=500)

    @web.middleware
    async def _no_cache(self, request, handler):
        response = await handler(request)
        if request.path.startswith("/static/") or not request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    @web.middleware
    async def _session(self, request, handler):
        request["session"] = self._decode(request.cookies.get(COOKIE, ""))
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.path.startswith("/api/"):
            if request.path == "/api/age-verification":
                return await handler(request)
            session = request["session"]
            if not session:
                raise StoreError("로그인이 필요합니다.", 401)
            if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), session.get("csrf", "")):
                raise StoreError("요청 인증값이 올바르지 않습니다.", 403)
        return await handler(request)

    def _encode(self, payload: dict) -> str:
        if not self.secret:
            raise StoreError("WEB_SESSION_SECRET 설정이 필요합니다.", 503)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        return f"{_b64(raw)}.{_b64(hmac.new(self.secret, raw, hashlib.sha256).digest())}"

    def _decode(self, value: str) -> dict | None:
        if not value or not self.secret:
            return None
        try:
            encoded, signature = value.split(".", 1)
            raw = _unb64(encoded)
            expected = hmac.new(self.secret, raw, hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _unb64(signature)):
                return None
            payload = json.loads(raw)
            return payload if int(payload.get("exp", 0)) > time.time() else None
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def _routes(self):
        routes = [
            web.get("/health", self.health),
            web.get("/auth/login", self.login),
            web.get("/auth/callback", self.callback),
            web.post("/api/logout", self.logout),
            web.get("/api/age-verification", self.age_verification),
            web.post("/api/age-verification", self.age_verification),
            web.get("/api/me", self.me),
            web.get(r"/api/products/{product_id:\d+}/image", self.product_image),
            web.get("/api/catalog", self.catalog),
            web.get("/api/reviews", self.reviews),
            web.post("/api/reviews", self.submit_review),
            web.get(r"/api/reviews/{review_id:\d+}/image/{image_index:\d+}", self.review_image),
            web.get("/api/purchase-ranking", self.purchase_ranking),
            web.get("/api/stores", self.stores),
            web.get("/api/tracking/carriers", self.tracking_carriers),
            web.post("/api/tracking", self.tracking),
            web.get("/api/customer", self.customer),
            web.put("/api/customer", self.save_customer),
            web.get("/api/cart", self.cart),
            web.post("/api/cart", self.save_cart),
            web.delete(r"/api/cart/{product_id:\d+}", self.delete_cart),
            web.post("/api/checkout", self.checkout),
            web.post("/api/tickets", self.create_web_ticket),
            web.get("/api/inquiries", self.inquiries),
            web.get("/api/chat", self.chat_sessions),
            web.post("/api/chat", self.create_chat_session),
            web.get(r"/api/chat/{session_id:\d+}", self.chat_session),
            web.post(r"/api/chat/{session_id:\d+}/messages", self.chat_message),
            web.post(r"/api/chat/{session_id:\d+}/close-choice", self.chat_close_choice),
            web.get(r"/api/chat/messages/{message_id:\d+}/image", self.chat_message_image),
            web.get("/api/orders", self.orders),
            web.get(r"/api/orders/{order_id}/tracking", self.order_tracking),
            web.get(r"/api/orders/{order_id}", self.order),
            web.get("/api/admin/dashboard", self.admin_dashboard),
            web.get("/api/admin/categories", self.admin_categories),
            web.post("/api/admin/categories", self.admin_save_category),
            web.delete(r"/api/admin/categories/{category_id:\d+}", self.admin_delete_category),
            web.get("/api/admin/products", self.admin_products),
            web.post("/api/admin/products", self.admin_save_product),
            web.post("/api/admin/products/import", self.admin_import_products),
            web.post("/api/admin/products/sync", self.admin_sync_products),
            web.post("/api/admin/products/delete-batch", self.admin_delete_products_batch),
            web.delete(r"/api/admin/products/{product_id:\d+}", self.admin_delete_product),
            web.get("/api/admin/channels", self.admin_channels),
            web.put("/api/admin/channels", self.admin_save_channels),
            web.get("/api/admin/orders", self.admin_orders),
            web.post(r"/api/admin/orders/{order_id}/cancel", self.admin_cancel_order),
            web.post(r"/api/admin/orders/{order_id}/waybill", self.admin_set_waybill),
            web.delete(r"/api/admin/orders/{order_id}", self.admin_delete_order),
            web.get("/api/admin/blacklist", self.admin_blacklist),
            web.post("/api/admin/blacklist", self.admin_add_blacklist),
            web.delete(r"/api/admin/blacklist/{user_id:\d+}", self.admin_remove_blacklist),
            web.get("/api/admin/users", self.admin_users),
            web.get("/api/admin/inquiries", self.admin_inquiries),
            web.post(r"/api/admin/inquiries/{inquiry_id:\d+}/answer", self.admin_answer_inquiry),
            web.delete(r"/api/admin/inquiries/{inquiry_id:\d+}", self.admin_delete_inquiry),
            web.get("/api/admin/direct-dm", self.admin_direct_dm),
            web.post("/api/admin/direct-dm", self.admin_send_direct_dm),
            web.get("/api/admin/subadmins", self.admin_subadmins),
            web.post("/api/admin/subadmins", self.admin_save_subadmin),
            web.delete(r"/api/admin/subadmins/{user_id:\d+}", self.admin_delete_subadmin),
            web.get("/api/subadmin/panel", self.subadmin_panel),
            web.post("/api/subadmin/products", self.admin_save_product),
            web.delete(r"/api/subadmin/products/{product_id:\d+}", self.admin_delete_product),
            web.post(r"/api/subadmin/orders/{order_id}/cancel", self.admin_cancel_order),
            web.post(r"/api/subadmin/orders/{order_id}/waybill", self.admin_set_waybill),
            web.delete(r"/api/subadmin/orders/{order_id}", self.admin_delete_order),
            web.post("/api/admin/points", self.admin_points),
            web.get("/api/notices", self.notices),
            web.get(r"/api/notices/{notice_type:[A-Za-z]+}/image", self.notice_image),
            web.get("/api/policies", self.policies),
            web.get("/api/admin/notices", self.admin_notices),
            web.put("/api/admin/notices", self.admin_save_notice),
            web.get("/api/admin/policies", self.admin_policies),
            web.put("/api/admin/policies", self.admin_save_policies),
            web.get("/api/coupons", self.coupons),
            web.post("/api/admin/coupons", self.admin_issue_coupon),
            web.get("/api/coupon-events", self.coupon_events),
            web.post(r"/api/coupon-events/{event_key}/claim", self.claim_coupon_event),
            web.get(r"/api/coupon-events/{event_key}/image", self.coupon_event_image),
            web.get("/api/admin/coupon-events", self.admin_coupon_events),
            web.post("/api/admin/coupon-events", self.admin_create_coupon_event),
            web.delete(r"/api/admin/coupon-events/{event_key}", self.admin_delete_coupon_event),
        ]
        self.app.add_routes(routes)
        self.app.router.add_static("/static", STATIC, show_index=False)
        self.app.router.add_get(r"/{tail:.*}", self.index)

    async def start(self):
        if self.guild_id:
            async with self.bot.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE web_categories SET guild_id=$1 WHERE guild_id=0",
                    self.guild_id,
                )
                await conn.execute(
                    "DELETE FROM web_settings WHERE guild_id=$1 AND setting_key='admin_password_hash'",
                    self.guild_id,
                )
                await conn.execute(
                    "UPDATE web_products SET guild_id=$1 WHERE guild_id=0",
                    self.guild_id,
                )
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        host = os.getenv("WEB_HOST", "0.0.0.0")
        port = int(os.getenv("PORT", os.getenv("WEB_PORT", "8080")))
        await web.TCPSite(self.runner, host, port).start()
        self.payment_service.start()
        print(f"✅ 웹 스토어 및 Pushbullet 결제 서버가 {host}:{port}에서 시작되었습니다.")

    async def close(self):
        await self.payment_service.close()
        if self.runner:
            await self.runner.cleanup()

    def _guild(self):
        guild = self.bot.get_guild(self.guild_id) if self.guild_id else None
        if guild is None and len(self.bot.guilds) == 1:
            guild = self.bot.guilds[0]
            self.guild_id = guild.id
        return guild

    @staticmethod
    def _review_from_payload(components, attachments):
        text_parts = []
        media_urls = []

        def collect(component):
            payload = component.to_dict() if hasattr(component, "to_dict") else component
            if not isinstance(payload, dict):
                return
            if payload.get("type") == 10 and payload.get("content"):
                text_parts.append(str(payload["content"]))
            if payload.get("type") == 12:
                for item in payload.get("items", []):
                    media = item.get("media", {}) if isinstance(item, dict) else {}
                    if isinstance(media, dict) and media.get("url"):
                        media_urls.append(str(media["url"]))
            for child in payload.get("components", []):
                collect(child)

        for component in components or []:
            collect(component)

        rating = 0
        review = ""
        for value in text_parts:
            plain = value.strip().strip("*").strip()
            if plain and set(plain) == {"⭐"}:
                rating = len(plain)
            elif "구매후기" not in plain and len(plain) >= 5:
                review = plain

        image_url = ""
        attachment_urls = {}
        for attachment in attachments or []:
            if isinstance(attachment, dict):
                content_type = str(attachment.get("content_type") or "")
                filename = str(attachment.get("filename") or "").lower()
                url = str(attachment.get("url") or attachment.get("proxy_url") or "")
                is_image = bool(attachment.get("width") and attachment.get("height"))
            else:
                content_type = getattr(attachment, "content_type", "") or ""
                filename = str(getattr(attachment, "filename", "")).lower()
                url = str(getattr(attachment, "url", "") or getattr(attachment, "proxy_url", ""))
                is_image = bool(getattr(attachment, "width", None) and getattr(attachment, "height", None))
            if filename and url:
                attachment_urls[filename] = url
            if is_image or content_type.startswith("image/") or filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                image_url = url
                break

        if not image_url:
            for media_url in media_urls:
                if media_url.startswith("attachment://"):
                    image_url = attachment_urls.get(media_url.removeprefix("attachment://").lower(), "")
                elif media_url.startswith(("https://", "http://")):
                    image_url = media_url
                if image_url:
                    break

        if rating not in range(1, 6) or len(review) < 5:
            return None
        return rating, review, image_url

    @classmethod
    def _review_from_message(cls, message):
        return cls._review_from_payload(
            getattr(message, "components", []),
            getattr(message, "attachments", []),
        )

    async def _sync_reviews_from_discord(self):
        if time.monotonic() - self._last_review_sync < REVIEW_SYNC_INTERVAL:
            return
        try:
            guild = self._guild()
            guild_id = guild.id if guild else self.guild_id
            if not guild_id:
                return
            async with self.bot.db_pool.acquire() as conn:
                channel_id = await conn.fetchval(
                    "SELECT review_channel_id FROM guild_settings WHERE guild_id=$1",
                    guild_id,
                )
            if not channel_id:
                return
            before = None
            while True:
                messages = await self.bot.http.logs_from(
                    int(channel_id), 100, before=before,
                )
                parsed_rows = []
                for message in messages:
                    parsed = self._review_from_payload(
                        message.get("components", []),
                        message.get("attachments", []),
                    )
                    if not parsed:
                        continue
                    rating, content, image_url = parsed
                    timestamp = str(message.get("timestamp") or "").replace("Z", "+00:00")
                    try:
                        created_at = datetime.fromisoformat(timestamp)
                    except ValueError:
                        created_at = None
                    parsed_rows.append((message, rating, content, image_url, created_at))
                if parsed_rows:
                    async with self.bot.db_pool.acquire() as conn:
                        for message, rating, content, image_url, created_at in parsed_rows:
                            await save_web_review(
                                conn, guild_id, int(message["id"]), None,
                                rating, content, image_url, created_at,
                            )
                if len(messages) < 100:
                    break
                before = discord.Object(id=int(messages[-1]["id"]))
            self._last_review_sync = time.monotonic()
        except Exception:
            LOGGER.exception("Discord 구매후기 동기화 실패: guild=%s", self.guild_id)

    async def _user(self, request, allow_blocked: bool = False) -> dict:
        session = request["session"]
        if not session:
            raise StoreError("Discord 로그인이 필요합니다.", 401)
        if not allow_blocked:
            async with self.bot.db_pool.acquire() as conn:
                reason = await conn.fetchval(
                    """SELECT reason FROM web_blacklist WHERE guild_id=$1 AND user_id=$2
                       AND blacklist_type='PERMANENT' AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP)""",
                    self.guild_id, int(session["id"]),
                )
            if reason:
                raise StoreError(f"블랙리스트에 등록되어 사이트를 이용할 수 없습니다. 사유: {reason}", 403)
        return session

    async def _admin_member(self, request) -> dict:
        session = await self._user(request)
        if int(session["id"]) != ADMIN_USER_ID:
            raise StoreError("관리자 전용 메뉴입니다.", 403)
        return session

    async def _admin(self, request) -> dict:
        return await self._admin_member(request)

    async def _subadmin_member(self, request) -> dict:
        session = await self._user(request)
        if int(session["id"]) == ADMIN_USER_ID:
            return session
        async with self.bot.db_pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM web_subadmins WHERE guild_id=$1 AND user_id=$2)",
                self.guild_id, int(session["id"]),
            )
        if not exists:
            raise StoreError("부관리자 전용 메뉴입니다.", 403)
        return session

    async def _product_manager(self, request, product_id: int) -> dict:
        session = await self._user(request)
        if int(session["id"]) == ADMIN_USER_ID:
            return session
        async with self.bot.db_pool.acquire() as conn:
            assigned = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM web_subadmin_products WHERE guild_id=$1 AND user_id=$2 AND product_id=$3)",
                self.guild_id, int(session["id"]), product_id,
            )
        if not assigned:
            raise StoreError("담당 상품만 관리할 수 있습니다.", 403)
        return session

    async def _order_manager(self, request, order_id: str) -> dict:
        session = await self._user(request)
        if int(session["id"]) == ADMIN_USER_ID:
            return session
        async with self.bot.db_pool.acquire() as conn:
            assigned = await conn.fetchval(
                """SELECT EXISTS(
                       SELECT 1 FROM web_order_items i
                       JOIN web_subadmin_products s ON s.guild_id=$2 AND s.user_id=$3 AND s.product_id=i.product_id
                       JOIN orders o ON o.order_id=i.order_id AND o.guild_id=$2 AND o.source='WEB'
                       WHERE i.order_id=$1
                   )""",
                order_id, self.guild_id, int(session["id"]),
            )
        if not assigned:
            raise StoreError("담당 상품이 포함된 주문만 관리할 수 있습니다.", 403)
        return session

    async def _body(self, request) -> dict:
        try:
            data = await request.json()
        except (json.JSONDecodeError, TypeError):
            raise StoreError("JSON 요청 형식이 올바르지 않습니다.")
        if not isinstance(data, dict):
            raise StoreError("요청 형식이 올바르지 않습니다.")
        return data

    async def health(self, request):
        return web.json_response({
            "ok": True,
            "pushbullet": self.payment_service.enabled,
            "payment_timeout_seconds": 300,
        })

    def _client_ip_hash(self, request) -> str:
        forwarded = request.headers.get("X-Forwarded-For", "")
        client_ip = forwarded.split(",", 1)[0].strip() if forwarded else ""
        client_ip = request.headers.get("CF-Connecting-IP", "").strip() or client_ip
        client_ip = client_ip or (request.remote or "unknown")
        key = self.secret or b"v4p3-age-verification"
        return hmac.new(key, client_ip.encode("utf-8", "ignore"), hashlib.sha256).hexdigest()

    async def age_verification(self, request):
        ip_hash = self._client_ip_hash(request)
        async with self.bot.db_pool.acquire() as conn:
            if request.method == "POST":
                await conn.execute(
                    """
                    INSERT INTO web_age_confirmations (guild_id, ip_hash)
                    VALUES ($1, $2)
                    ON CONFLICT (guild_id, ip_hash) DO NOTHING
                    """,
                    self.guild_id, ip_hash,
                )
                return web.json_response({"verified": True})
            verified = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM web_age_confirmations WHERE guild_id=$1 AND ip_hash=$2)",
                self.guild_id, ip_hash,
            )
        return web.json_response({"verified": bool(verified)})

    async def index(self, request):
        return web.FileResponse(STATIC / "index.html")

    async def login(self, request):
        if not all((self.client_id, self.client_secret, self.redirect_uri, self.secret)):
            raise StoreError("Discord OAuth 환경변수가 설정되지 않았습니다.", 503)
        state = secrets.token_urlsafe(24)
        payload = {"state": state, "exp": int(time.time()) + 600}
        query = urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "identify guilds.join",
            "state": state,
            "prompt": "consent",
        })
        response = web.HTTPFound(f"https://discord.com/oauth2/authorize?{query}")
        response.set_cookie("v4p3_oauth", self._encode(payload), httponly=True, secure=True, samesite="Lax", max_age=600)
        return response

    async def callback(self, request):
        state_data = self._decode(request.cookies.get("v4p3_oauth", ""))
        if not state_data or not hmac.compare_digest(request.query.get("state", ""), state_data.get("state", "")):
            raise StoreError("Discord 로그인 요청이 만료되었거나 올바르지 않습니다.", 403)
        code = request.query.get("code")
        if not code:
            raise StoreError("Discord 로그인 승인이 취소되었습니다.", 400)
        async with aiohttp.ClientSession() as session:
            async with session.post("https://discord.com/api/v10/oauth2/token", data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            }) as token_response:
                token = await token_response.json()
                if token_response.status != 200:
                    raise StoreError("Discord 로그인 토큰을 발급받지 못했습니다.", 502)
            async with session.get("https://discord.com/api/v10/users/@me", headers={
                "Authorization": f"Bearer {token['access_token']}"
            }) as user_response:
                user = await user_response.json()
                if user_response.status != 200:
                    raise StoreError("Discord 사용자 정보를 확인하지 못했습니다.", 502)
        guild = self._guild()
        if guild is None:
            raise StoreError("판매 Discord 서버를 찾을 수 없습니다.", 503)
        try:
            member = guild.get_member(int(user["id"])) or await guild.fetch_member(int(user["id"]))
        except Exception:
            bot_token = os.getenv("BOT_TOKEN", "").strip()
            if not bot_token:
                raise StoreError("Discord 서버 자동 가입에 필요한 BOT_TOKEN이 설정되지 않았습니다.", 503)
            async with aiohttp.ClientSession() as session:
                async with session.put(
                    f"https://discord.com/api/v10/guilds/{guild.id}/members/{user['id']}",
                    headers={"Authorization": f"Bot {bot_token}"},
                    json={"access_token": token["access_token"]},
                ) as join_response:
                    if join_response.status not in {201, 204}:
                        LOGGER.warning(
                            "Discord 서버 자동 가입 실패: guild=%s user=%s status=%s body=%s",
                            guild.id, user["id"], join_response.status,
                            (await join_response.text())[:300],
                        )
                        raise StoreError(
                            "Discord 서버 자동 가입에 실패했습니다. 잠시 후 다시 로그인해 주세요.",
                            502,
                        )
            try:
                member = guild.get_member(int(user["id"])) or await guild.fetch_member(int(user["id"]))
            except Exception as exc:
                raise StoreError("Discord 서버 가입 확인에 실패했습니다. 다시 로그인해 주세요.", 502) from exc
        csrf = secrets.token_urlsafe(24)
        payload = {
            "id": str(user["id"]),
            "username": member.display_name,
            "avatar": user.get("avatar"),
            "csrf": csrf,
            "exp": int(time.time()) + 60 * 60 * 24 * 7,
        }
        response = web.HTTPFound(self.public_url or "/")
        response.set_cookie(COOKIE, self._encode(payload), httponly=True, secure=True, samesite="Lax", max_age=604800)
        response.del_cookie("v4p3_oauth")
        return response

    async def logout(self, request):
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE)
        return response

    async def me(self, request):
        user = await self._user(request, allow_blocked=True)
        is_admin = True
        try:
            await self._admin_member(request)
        except StoreError:
            is_admin = False
        async with self.bot.db_pool.acquire() as conn:
            blocked = await conn.fetchrow(
                """SELECT reason FROM web_blacklist WHERE guild_id=$1 AND user_id=$2
                   AND blacklist_type='PERMANENT' AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP)""",
                self.guild_id, int(user["id"]),
            )
            is_subadmin = is_admin or bool(await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM web_subadmins WHERE guild_id=$1 AND user_id=$2)",
                self.guild_id, int(user["id"]),
            ))
        return web.json_response({"user": {"id": user["id"], "username": user["username"], "avatar": user.get("avatar")}, "csrf": user["csrf"], "is_admin": is_admin, "is_subadmin": is_subadmin, "blocked": dict(blocked) if blocked else None})

    async def product_image(self, request):
        product_id = int(request.match_info["product_id"])
        async with self.bot.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT mime_type,content_hash,image_data
                FROM web_product_images
                WHERE product_id=$1 AND guild_id=$2
                """,
                product_id, self.guild_id,
            )
        if not row:
            raise web.HTTPNotFound(text="상품 이미지를 찾을 수 없습니다.")
        etag = f'"{row["content_hash"]}"'
        cache_headers = {
            "ETag": etag,
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        }
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=cache_headers)
        return web.Response(
            body=bytes(row["image_data"]),
            content_type=row["mime_type"],
            headers=cache_headers,
        )

    async def catalog(self, request):
        guild = self._guild()
        guild_id = guild.id if guild else self.guild_id
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT p.*, c.name AS category_name, c.parent_id,
                       parent.name AS parent_category_name
                FROM web_products p
                LEFT JOIN web_categories c ON c.id = p.category_id
                LEFT JOIN web_categories parent ON parent.id = c.parent_id
                WHERE p.guild_id = $1 AND p.is_active = TRUE
                ORDER BY COALESCE(parent.sort_order, c.sort_order), parent.name,
                         c.sort_order, c.name, p.name
                """, guild_id,
            )
            categories = await conn.fetch(
                """
                SELECT c.id, c.name, c.parent_id, c.sort_order,
                       parent.name AS parent_name
                FROM web_categories c
                LEFT JOIN web_categories parent ON parent.id = c.parent_id
                WHERE c.guild_id=$1
                ORDER BY COALESCE(parent.sort_order, c.sort_order), parent.name,
                         c.parent_id NULLS FIRST, c.sort_order, c.name
                """,
                guild_id,
            )
        return web.json_response(
            {"products": [dict(row) for row in rows], "categories": [dict(row) for row in categories]},
            dumps=lambda value: json.dumps(value, ensure_ascii=False, default=str),
        )

    async def reviews(self, request):
        await self._sync_reviews_from_discord()
        await self._rewatermark_existing_reviews()
        guild = self._guild()
        guild_id = guild.id if guild else self.guild_id
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT discord_message_id AS id, rating, content, image_url, created_at
                FROM web_reviews
                WHERE guild_id=$1 AND CHAR_LENGTH(BTRIM(content)) >= 5
                ORDER BY created_at DESC
                """,
                guild_id,
            )
        return web.json_response(
            {"reviews": [dict(row) for row in rows]},
            dumps=lambda value: json.dumps(value, ensure_ascii=False, default=str),
        )

    async def _rewatermark_existing_reviews(self):
        guild = self._guild()
        guild_id = guild.id if guild else self.guild_id
        if not guild_id:
            return
        if self._review_watermark_lock.locked():
            async with self._review_watermark_lock:
                return
        async with self._review_watermark_lock:
            async with self.bot.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT discord_message_id,image_url FROM web_reviews
                       WHERE guild_id=$1 AND watermark_version < 1 AND BTRIM(image_url) <> ''
                       ORDER BY created_at""",
                    guild_id,
                )
            if not rows:
                return
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for row in rows:
                    review_id = int(row["discord_message_id"])
                    try:
                        async with self.bot.db_pool.acquire() as conn:
                            stored_images = await conn.fetch(
                                "SELECT image_index,image_data FROM web_review_images WHERE guild_id=$1 AND review_id=$2 ORDER BY image_index",
                                guild_id, review_id,
                            )
                        if stored_images:
                            sources = [(int(item["image_index"]), bytes(item["image_data"])) for item in stored_images]
                        elif str(row["image_url"]).startswith(("https://", "http://")):
                            async with session.get(str(row["image_url"])) as response:
                                if response.status != 200:
                                    raise StoreError(f"후기 이미지 응답 HTTP {response.status}")
                                sources = [(0, await response.read())]
                        else:
                            continue
                        rendered_images = [
                            (index, await asyncio.to_thread(watermark_review_image, source))
                            for index, source in sources
                        ]
                        local_url = f"/api/reviews/{review_id}/image/0"
                        async with self.bot.db_pool.acquire() as conn:
                            async with conn.transaction():
                                for image_index, rendered in rendered_images:
                                    await conn.execute(
                                        """INSERT INTO web_review_images (guild_id,review_id,image_index,image_data)
                                           VALUES ($1,$2,$3,$4)
                                           ON CONFLICT (guild_id,review_id,image_index)
                                           DO UPDATE SET image_data=EXCLUDED.image_data""",
                                        guild_id, review_id, image_index, rendered,
                                    )
                                await conn.execute(
                                    "UPDATE web_reviews SET image_url=$3,watermark_version=1 WHERE guild_id=$1 AND discord_message_id=$2",
                                    guild_id, review_id, local_url,
                                )
                    except Exception:
                        LOGGER.exception("기존 후기 워터마크 재적용 실패: review=%s", review_id)

    async def review_image(self, request):
        async with self.bot.db_pool.acquire() as conn:
            data = await conn.fetchval(
                "SELECT image_data FROM web_review_images WHERE guild_id=$1 AND review_id=$2 AND image_index=$3",
                self.guild_id, int(request.match_info["review_id"]), int(request.match_info["image_index"]),
            )
        if not data:
            raise web.HTTPNotFound()
        return web.Response(body=bytes(data), content_type="image/jpeg", headers={"Cache-Control": "public,max-age=31536000"})

    async def submit_review(self, request):
        user, data = await self._user(request), await self._body(request)
        content = str(data.get("content", "")).strip()
        rating = int(data.get("rating", 0) or 0)
        images = data.get("images", [])
        if rating not in range(1, 6):
            raise StoreError("별점을 선택해 주세요.")
        if len(content) < 5:
            raise StoreError("후기는 5자 이상 작성해 주세요.")
        if not isinstance(images, list) or len(images) > 4:
            raise StoreError("후기 사진은 최대 4장까지 등록할 수 있습니다.")
        processed = []
        for encoded in images:
            try:
                raw = base64.b64decode(str(encoded).split(",", 1)[-1], validate=True)
                processed.append(await asyncio.to_thread(watermark_review_image, raw))
            except (ValueError, ReviewImageError) as exc:
                raise StoreError(str(exc)) from exc
        review_id = int(time.time() * 1000) * 1000 + secrets.randbelow(1000)
        async with self.bot.db_pool.acquire() as conn:
            has_order = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM orders WHERE guild_id=$1 AND buyer_id=$2 AND source='WEB' AND status='APPROVED')",
                self.guild_id, int(user["id"]),
            )
            if not has_order:
                raise StoreError("구매가 승인된 고객만 후기를 작성할 수 있습니다.", 403)
            image_url = f"/api/reviews/{review_id}/image/0" if processed else ""
            await save_web_review(conn, self.guild_id, review_id, int(user["id"]), rating, content, image_url)
            for index, image in enumerate(processed):
                await conn.execute(
                    "INSERT INTO web_review_images (guild_id,review_id,image_index,image_data) VALUES ($1,$2,$3,$4)",
                    self.guild_id, review_id, index, image,
                )
            await conn.execute(
                "UPDATE web_reviews SET watermark_version=1 WHERE guild_id=$1 AND discord_message_id=$2",
                self.guild_id, review_id,
            )
            try:
                reward, balance = await award_review_points(conn, self.guild_id, int(user["id"]), review_id)
            except Exception:
                reward, balance = 0, await get_balance(conn, self.guild_id, int(user["id"]))
            channel_id = await conn.fetchval("SELECT review_channel_id FROM guild_settings WHERE guild_id=$1", self.guild_id)
        if channel_id:
            channel = self.bot.get_channel(int(channel_id))
            if channel:
                try:
                    files = [discord.File(BytesIO(image), filename=f"review-{review_id}-{index}.jpg") for index, image in enumerate(processed)]
                    await channel.send(
                        f"## 구매후기\n- 구매자 : 익명\n- 별점 : {'⭐' * rating}\n\n**{content}**",
                        files=files,
                    )
                except Exception:
                    LOGGER.exception("웹 구매후기 Discord 전송 실패: %s", review_id)
        return web.json_response({"ok": True, "reward": reward, "balance": balance})

    async def purchase_ranking(self, request):
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id AS buyer_id, total_spent::BIGINT total
                FROM user_info WHERE total_spent > 0
                ORDER BY total_spent DESC, user_id ASC LIMIT 100
                """,
            )
        guild = self._guild()
        ranking = []
        for row in rows:
            member = guild.get_member(int(row["buyer_id"])) if guild else None
            ranking.append({"name": member.display_name if member else "탈퇴한 사용자", "total": int(row["total"])})
        return web.json_response({"ranking": ranking}, dumps=lambda value: json.dumps(value, ensure_ascii=False))

    async def stores(self, request):
        await self._user(request)
        brand = str(request.query.get("brand", "")).strip().upper()
        query = str(request.query.get("q", "")).strip()
        try:
            results = await search_stores(brand, query)
        except StoreLookupError as exc:
            raise StoreError(str(exc)) from exc
        return web.json_response({"stores": results[:25]}, dumps=lambda value: json.dumps(value, ensure_ascii=False))

    async def tracking_carriers(self, request):
        await self._user(request)
        carriers = [
            {"id": carrier["id"], "name": carrier["name"]}
            for carrier in TRACKING_PANEL_CARRIERS
        ]
        return web.json_response({"carriers": carriers}, dumps=lambda value: json.dumps(value, ensure_ascii=False))

    async def tracking(self, request):
        await self._user(request)
        data = await self._body(request)
        carrier_id = str(data.get("carrier_id", "")).strip()
        try:
            waybill = normalize_waybill(str(data.get("waybill", "")))
            payload = await self._tracking_payload(carrier_id, waybill)
        except DeliveryTrackingError as exc:
            raise StoreError(str(exc), 400) from exc
        return web.json_response(payload, dumps=lambda value: json.dumps(value, ensure_ascii=False))

    async def _tracking_payload(self, carrier_id: str, waybill: str):
        carrier = next((item for item in TRACKING_PANEL_CARRIERS if item["id"] == carrier_id), None)
        if carrier is None:
            raise DeliveryTrackingError("택배사를 다시 선택해 주세요.")
        result = await track_shipment(carrier_id, waybill)

        def text_value(value, fallback=""):
            if isinstance(value, dict):
                value = value.get("text") or value.get("name") or value.get("status") or fallback
            return str(value).strip()[:300] if value is not None else fallback

        history = []
        for item in reversed(result.get("allProgress") or []):
            location = text_value(item.get("location"), "")
            history.append({
                "time": text_value(item.get("time"), ""),
                "location": location,
                "status": text_value(item.get("status"), ""),
                "description": text_value(item.get("description"), ""),
            })
        status = text_value(result.get("status"), "배송 상태 확인 중")
        delivery_words = " ".join(
            [status] + [f"{item['status']} {item['description']}" for item in history[:3]]
        ).lower()
        delivered = any(word in delivery_words for word in ("배송완료", "배달완료", "전달완료", "delivered"))
        return {
            "carrier_name": carrier["name"],
            "waybill": waybill,
            "delivered": delivered,
            "shipment": {
                "status": status,
                "location": text_value(result.get("location"), ""),
                "receiver": text_value(result.get("receiver"), ""),
                "sender": text_value(result.get("sender"), ""),
                "history": history[:30],
            },
        }

    async def customer(self, request):
        user = await self._user(request)
        async with self.bot.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT name,contact,address,cvs,cvs_brand,cvs_address,refund_bank,
                          refund_account_number,refund_account_holder,refund_policy_version
                   FROM user_info WHERE user_id = $1""",
                int(user["id"]),
            )
            balance = await get_balance(conn, self.guild_id, int(user["id"]))
            policy = await self._policy_data(conn)
        customer = dict(row) if row else {}
        accepted = bool(customer.get("refund_policy_version") == policy["refund_policy_version"])
        customer["refund_policy_accepted"] = accepted
        return web.json_response({
            "customer": customer,
            "points": balance,
            "refund_policy": {
                "content": policy["refund_policy"],
                "version": policy["refund_policy_version"],
                "accepted": accepted,
            },
        }, dumps=lambda value: json.dumps(value, ensure_ascii=False))

    async def save_customer(self, request):
        user, data = await self._user(request), await self._body(request)
        keys = ("name", "contact", "address", "cvs", "cvs_brand", "cvs_address", "refund_bank", "refund_account_number", "refund_account_holder")
        values = [str(data.get(key, "")).strip() for key in keys]
        if not values[0] or not re.fullmatch(r"010-\d{4}-\d{4}", values[1]):
            raise StoreError("실명과 010-1234-5678 형식의 전화번호를 입력해 주세요.")
        if not values[2] and not (values[3] and values[4] in {"GS25", "CU"} and values[5]):
            raise StoreError("집주소 또는 조회한 편의점 정보를 입력해 주세요.")
        async with self.bot.db_pool.acquire() as conn:
            policy = await self._policy_data(conn)
            submitted_version = str(data.get("refund_policy_version") or "")
            if not data.get("refund_policy_accepted") or not hmac.compare_digest(submitted_version, policy["refund_policy_version"]):
                raise StoreError("환불약관을 끝까지 확인하고 동의해 주세요.")
            if not values[6] or not values[7] or not values[8]:
                raise StoreError("환불 은행, 계좌번호와 예금주를 모두 입력해 주세요.")
            if not re.fullmatch(r"[0-9-]{6,30}", values[7]):
                raise StoreError("환불 계좌번호는 숫자와 하이픈만 사용해 주세요.")
            combined_refund_account = f"{values[6]} / {values[7]} / {values[8]}"
            await conn.execute(
                """
                INSERT INTO user_info (user_id,name,contact,address,cvs,cvs_brand,cvs_address,refund_bank,
                                      refund_account_number,refund_account_holder,refund_policy_version,refund_account)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                ON CONFLICT (user_id) DO UPDATE SET
                    name = EXCLUDED.name, contact = EXCLUDED.contact,
                    address = EXCLUDED.address, cvs = EXCLUDED.cvs,
                    cvs_brand=EXCLUDED.cvs_brand,cvs_address=EXCLUDED.cvs_address,
                    refund_bank=EXCLUDED.refund_bank,refund_account_number=EXCLUDED.refund_account_number,
                    refund_account_holder=EXCLUDED.refund_account_holder,
                    refund_policy_version=EXCLUDED.refund_policy_version,refund_account=EXCLUDED.refund_account
                """, int(user["id"]), *values, policy["refund_policy_version"], combined_refund_account,
            )
        return web.json_response({"ok": True})

    async def admin_users(self, request):
        await self._admin(request)
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT u.user_id,u.name,u.contact,u.address,u.cvs,u.cvs_brand,u.cvs_address,
                          u.refund_bank,u.refund_account_number,u.refund_account_holder,
                          COALESCE(u.total_spent,0) total_spent,COALESCE(p.balance,0) points
                   FROM user_info u LEFT JOIN point_balances p ON p.guild_id=$1 AND p.user_id=u.user_id
                   ORDER BY u.user_id DESC""",
                self.guild_id,
            )
        profiles = await asyncio.gather(*(self._blacklist_profile(int(row["user_id"])) for row in rows))
        users = [{**dict(row), **profile, "user_id": str(row["user_id"])} for row, profile in zip(rows, profiles)]
        return web.json_response({"users": users}, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=str))

    async def admin_subadmins(self, request):
        await self._admin(request)
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT s.user_id,s.created_at,COALESCE(array_agg(p.product_id) FILTER (WHERE p.product_id IS NOT NULL),'{}') product_ids FROM web_subadmins s LEFT JOIN web_subadmin_products p ON p.guild_id=s.guild_id AND p.user_id=s.user_id WHERE s.guild_id=$1 GROUP BY s.user_id,s.created_at ORDER BY s.created_at DESC", self.guild_id)
            products = await conn.fetch("SELECT id,name FROM web_products WHERE guild_id=$1 AND is_active=TRUE ORDER BY name", self.guild_id)
        entries = []
        for row in rows:
            profile = await self._blacklist_profile(int(row["user_id"]))
            entries.append({**dict(row), **profile, "user_id": str(row["user_id"]), "product_ids": [str(value) for value in row["product_ids"]]})
        return web.json_response({"subadmins": entries, "products": [dict(row) for row in products]}, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=str))

    async def subadmin_panel(self, request):
        member = await self._subadmin_member(request)
        is_admin = int(member["id"]) == ADMIN_USER_ID
        async with self.bot.db_pool.acquire() as conn:
            categories = await conn.fetch(
                "SELECT id,name,parent_id,sort_order FROM web_categories WHERE guild_id=$1 ORDER BY sort_order,name",
                self.guild_id,
            )
            if is_admin:
                products = await conn.fetch(
                    """SELECT p.*,c.name category_name,c.parent_id,parent.name parent_category_name
                       FROM web_products p LEFT JOIN web_categories c ON c.id=p.category_id
                       LEFT JOIN web_categories parent ON parent.id=c.parent_id
                       WHERE p.guild_id=$1 ORDER BY p.updated_at DESC""",
                    self.guild_id,
                )
                orders = await conn.fetch(
                    """SELECT order_id,buyer_id,product,quantity,amount,status,created_at,processed_at,
                              shipping_name,shipping_contact,shipping_address,shipping_cvs,shipping_method,shipping_memo,
                              shipping_fee,points_used,cash_amount,waybill_number,carrier_id,
                              fulfillment_status,fulfillment_updated_at,cancel_reason
                       FROM orders WHERE guild_id=$1 AND source='WEB'
                       ORDER BY created_at DESC LIMIT 200""",
                    self.guild_id,
                )
            else:
                products = await conn.fetch(
                    """SELECT p.*,c.name category_name,c.parent_id,parent.name parent_category_name
                       FROM web_subadmin_products s
                       JOIN web_products p ON p.id=s.product_id AND p.guild_id=s.guild_id
                       LEFT JOIN web_categories c ON c.id=p.category_id
                       LEFT JOIN web_categories parent ON parent.id=c.parent_id
                       WHERE s.guild_id=$1 AND s.user_id=$2
                       ORDER BY p.updated_at DESC""",
                    self.guild_id, int(member["id"]),
                )
                orders = await conn.fetch(
                    """SELECT DISTINCT o.order_id,o.buyer_id,o.product,o.quantity,o.amount,o.status,o.created_at,o.processed_at,
                              o.shipping_name,o.shipping_contact,o.shipping_address,o.shipping_cvs,o.shipping_method,o.shipping_memo,
                              o.shipping_fee,o.points_used,o.cash_amount,o.waybill_number,o.carrier_id,
                              o.fulfillment_status,o.fulfillment_updated_at,o.cancel_reason
                       FROM orders o JOIN web_order_items i ON i.order_id=o.order_id
                       JOIN web_subadmin_products s ON s.guild_id=o.guild_id AND s.user_id=$2 AND s.product_id=i.product_id
                       WHERE o.guild_id=$1 AND o.source='WEB'
                       ORDER BY o.created_at DESC LIMIT 200""",
                    self.guild_id, int(member["id"]),
                )
        guild = self._guild()
        serialized_orders = []
        for row in orders:
            item = dict(row)
            buyer = guild.get_member(int(row["buyer_id"])) if guild else None
            item["buyer_name"] = buyer.display_name if buyer else str(row["buyer_id"])
            serialized_orders.append(item)
        return web.json_response(
            {"is_admin": is_admin, "categories": [dict(row) for row in categories], "products": [dict(row) for row in products], "orders": serialized_orders},
            dumps=lambda value: json.dumps(value, ensure_ascii=False, default=str),
        )

    async def admin_save_subadmin(self, request):
        admin, data = await self._admin(request), await self._body(request)
        user_id = self._parse_discord_user_id(data.get("user_id"))
        product_ids = list(dict.fromkeys(int(value) for value in data.get("product_ids", []) if str(value).isdigit()))
        if not product_ids:
            raise StoreError("부관리자가 담당할 상품을 한 개 이상 선택해 주세요.")
        profile = await self._blacklist_profile(user_id)
        if profile["username"] == "알 수 없음": raise StoreError("Discord 사용자를 찾을 수 없습니다.", 404)
        async with self.bot.db_pool.acquire() as conn:
            valid = await conn.fetchval("SELECT COUNT(*) FROM web_products WHERE guild_id=$1 AND id=ANY($2::bigint[])", self.guild_id, product_ids)
            if int(valid or 0) != len(product_ids): raise StoreError("선택한 상품 일부를 찾을 수 없습니다.")
            async with conn.transaction():
                await conn.execute("INSERT INTO web_subadmins (guild_id,user_id,added_by) VALUES ($1,$2,$3) ON CONFLICT (guild_id,user_id) DO UPDATE SET added_by=EXCLUDED.added_by", self.guild_id, user_id, int(admin["id"]))
                await conn.execute("DELETE FROM web_subadmin_products WHERE guild_id=$1 AND user_id=$2", self.guild_id, user_id)
                await conn.executemany("INSERT INTO web_subadmin_products (guild_id,user_id,product_id) VALUES ($1,$2,$3)", [(self.guild_id, user_id, product_id) for product_id in product_ids])
        return web.json_response({"ok": True})

    async def admin_delete_subadmin(self, request):
        await self._admin(request); user_id = self._parse_discord_user_id(request.match_info["user_id"])
        async with self.bot.db_pool.acquire() as conn:
            result = await conn.execute("DELETE FROM web_subadmins WHERE guild_id=$1 AND user_id=$2", self.guild_id, user_id)
        if result == "DELETE 0": raise StoreError("부관리자를 찾을 수 없습니다.", 404)
        return web.json_response({"ok": True})

    async def admin_points(self, request):
        admin = await self._admin(request); data = await self._body(request)
        try:
            amount = int(data.get("amount") or 0)
        except (TypeError, ValueError) as exc:
            raise StoreError("포인트를 숫자로 입력해 주세요.") from exc
        if amount == 0:
            raise StoreError("0이 아닌 포인트를 입력해 주세요.")
        async with self.bot.db_pool.acquire() as conn:
            if data.get("all_users"):
                ids = await conn.fetch("SELECT DISTINCT user_id FROM user_info")
                for index, row in enumerate(ids):
                    await adjust_points(conn, self.guild_id, int(row["user_id"]), int(admin["id"]), time.time_ns() + index, amount)
                return web.json_response({"ok": True, "count": len(ids)})
            user_id = self._parse_discord_user_id(data.get("user_id"))
            balance = await adjust_points(conn, self.guild_id, user_id, int(admin["id"]), time.time_ns(), amount)
        return web.json_response({"ok": True, "balance": balance})

    async def _policy_data(self, conn) -> dict:
        rows = await conn.fetch(
            "SELECT setting_key,setting_value FROM web_settings WHERE guild_id=$1 AND setting_key=ANY($2::text[])",
            self.guild_id, ["terms_of_service", "refund_policy"],
        )
        values = {row["setting_key"]: row["setting_value"] for row in rows}
        terms = str(values.get("terms_of_service") or DEFAULT_TERMS_OF_SERVICE).strip()
        refund = str(values.get("refund_policy") or DEFAULT_REFUND_POLICY).strip()
        return {
            "terms_of_service": terms,
            "refund_policy": refund,
            "refund_policy_version": hashlib.sha256(refund.encode("utf-8")).hexdigest(),
        }

    async def policies(self, request):
        async with self.bot.db_pool.acquire() as conn:
            data = await self._policy_data(conn)
        return web.json_response(data, dumps=lambda value: json.dumps(value, ensure_ascii=False))

    async def admin_policies(self, request):
        await self._admin(request)
        async with self.bot.db_pool.acquire() as conn:
            data = await self._policy_data(conn)
        return web.json_response(data, dumps=lambda value: json.dumps(value, ensure_ascii=False))

    async def admin_save_policies(self, request):
        await self._admin(request)
        data = await self._body(request)
        terms = str(data.get("terms_of_service") or "").strip()
        refund = str(data.get("refund_policy") or "").strip()
        if len(terms) < 20 or len(refund) < 20:
            raise StoreError("이용약관과 환불약관을 각각 20자 이상 입력해 주세요.")
        if len(terms) > 20_000 or len(refund) > 20_000:
            raise StoreError("약관은 각각 20,000자 이하로 입력해 주세요.")
        async with self.bot.db_pool.acquire() as conn:
            async with conn.transaction():
                for key, value in (("terms_of_service", terms), ("refund_policy", refund)):
                    await conn.execute(
                        """INSERT INTO web_settings (guild_id,setting_key,setting_value)
                           VALUES ($1,$2,$3)
                           ON CONFLICT (guild_id,setting_key) DO UPDATE
                           SET setting_value=EXCLUDED.setting_value,updated_at=CURRENT_TIMESTAMP""",
                        self.guild_id, key, value,
                    )
        return web.json_response({"ok": True})

    async def notices(self, request):
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT notice_type,content,updated_at,(image_data IS NOT NULL) has_image FROM web_notices WHERE guild_id=$1 AND is_active=TRUE",
                self.guild_id,
            )
        notices = []
        for row in rows:
            item = dict(row)
            item["image_url"] = f"/api/notices/{item['notice_type']}/image" if item.pop("has_image") else ""
            notices.append(item)
        return web.json_response({"notices": notices}, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=str))

    async def notice_image(self, request):
        notice_type = str(request.match_info["notice_type"] or "").upper()
        if notice_type not in {"NOTICE", "EVENT"}:
            raise web.HTTPNotFound()
        async with self.bot.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT image_mime,image_data FROM web_notices WHERE guild_id=$1 AND notice_type=$2 AND image_data IS NOT NULL",
                self.guild_id, notice_type,
            )
        if not row:
            raise web.HTTPNotFound()
        return web.Response(
            body=bytes(row["image_data"]), content_type=row["image_mime"],
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def admin_notices(self, request):
        await self._admin(request)
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT notice_type,content,is_active,updated_at,(image_data IS NOT NULL) has_image FROM web_notices WHERE guild_id=$1",
                self.guild_id,
            )
        notices = []
        for row in rows:
            item = dict(row)
            item["image_url"] = f"/api/notices/{item['notice_type']}/image" if item.pop("has_image") else ""
            notices.append(item)
        return web.json_response({"notices": notices}, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=str))

    async def admin_save_notice(self, request):
        await self._admin(request); data = await self._body(request)
        notice_type = str(data.get("notice_type") or "NOTICE").upper()
        if notice_type not in {"NOTICE", "EVENT"}:
            raise StoreError("공지 종류가 올바르지 않습니다.")
        content = str(data.get("content") or "").strip()
        active = bool(data.get("is_active"))
        if active and len(content) < 2:
            raise StoreError("내용을 2자 이상 입력해 주세요.")
        image_mime = None
        image_data = None
        image_value = str(data.get("image_data_url") or "")
        replace_image = bool(data.get("remove_image"))
        if image_value:
            match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64,(.+)", image_value, re.DOTALL)
            if not match:
                raise StoreError("공지 이미지는 PNG, JPG 또는 WEBP 파일만 사용할 수 있습니다.")
            try:
                image_data = base64.b64decode(match.group(2), validate=True)
            except (ValueError, TypeError) as exc:
                raise StoreError("공지 이미지 데이터를 읽을 수 없습니다.") from exc
            if not image_data or len(image_data) > 5 * 1024 * 1024:
                raise StoreError("공지 이미지는 5MB 이하로 업로드해 주세요.")
            image_mime = match.group(1)
            replace_image = True
        async with self.bot.db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO web_notices (guild_id,notice_type,content,is_active,image_mime,image_data)
                   VALUES ($1,$2,$3,$4,$5,$6)
                   ON CONFLICT (guild_id,notice_type) DO UPDATE SET
                     content=EXCLUDED.content,is_active=EXCLUDED.is_active,
                     image_mime=CASE WHEN $7 THEN EXCLUDED.image_mime ELSE web_notices.image_mime END,
                     image_data=CASE WHEN $7 THEN EXCLUDED.image_data ELSE web_notices.image_data END,
                     updated_at=CURRENT_TIMESTAMP""",
                self.guild_id, notice_type, content, active, image_mime, image_data, replace_image,
            )
        return web.json_response({"ok": True})

    async def coupons(self, request):
        user = await self._user(request)
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT code,discount_amount,expires_at,min_order_amount FROM web_coupons
                   WHERE guild_id=$1 AND user_id=$2 AND used_order_id IS NULL
                     AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP)
                   ORDER BY expires_at NULLS LAST,created_at""",
                self.guild_id, int(user["id"]),
            )
        return web.json_response({"coupons": [dict(row) for row in rows]}, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=str))

    async def admin_issue_coupon(self, request):
        await self._admin(request); data = await self._body(request)
        try:
            amount = int(data.get("discount_amount") or 0)
        except (TypeError, ValueError) as exc:
            raise StoreError("쿠폰 금액을 숫자로 입력해 주세요.") from exc
        if amount <= 0:
            raise StoreError("쿠폰 금액은 1원 이상이어야 합니다.")
        try:
            days = max(1, min(int(data.get("days") or 30), 365))
            min_order_amount = max(0, int(data.get("min_order_amount") or 0))
        except (TypeError, ValueError) as exc:
            raise StoreError("쿠폰 사용 기한과 최소 주문금액을 숫자로 입력해 주세요.") from exc
        expires_at = datetime.now().astimezone() + timedelta(days=days)
        async with self.bot.db_pool.acquire() as conn:
            if data.get("all_users"):
                users = await conn.fetch("SELECT DISTINCT user_id FROM user_info ORDER BY user_id")
                if not users:
                    raise StoreError("쿠폰을 지급할 등록 유저가 없습니다.")
                codes = []
                async with conn.transaction():
                    for row in users:
                        code = f"V4P3-{secrets.token_hex(4).upper()}"
                        await conn.execute(
                            "INSERT INTO web_coupons (guild_id,code,user_id,discount_amount,expires_at,min_order_amount) VALUES ($1,$2,$3,$4,$5,$6)",
                            self.guild_id, code, int(row["user_id"]), amount, expires_at, min_order_amount,
                        )
                        codes.append(code)
                return web.json_response({"ok": True, "count": len(codes)})
            user_id = self._parse_discord_user_id(data.get("user_id"))
            code = f"V4P3-{secrets.token_hex(4).upper()}"
            await conn.execute(
                "INSERT INTO web_coupons (guild_id,code,user_id,discount_amount,expires_at,min_order_amount) VALUES ($1,$2,$3,$4,$5,$6)",
                self.guild_id, code, user_id, amount, expires_at, min_order_amount,
            )
        return web.json_response({"ok": True, "code": code, "count": 1})

    async def coupon_events(self, request):
        user = await self._user(request)
        user_id = int(user["id"])
        async with self.bot.db_pool.acquire() as conn:
            total_spent = int(await conn.fetchval(
                "SELECT COALESCE(total_spent,0) FROM user_info WHERE user_id=$1", user_id,
            ) or 0)
            purchased = bool(await conn.fetchval(
                """SELECT EXISTS(SELECT 1 FROM orders WHERE guild_id=$1 AND buyer_id=$2 AND status='APPROVED')
                          OR COALESCE((SELECT total_spent > 0 FROM user_info WHERE user_id=$2),FALSE)""",
                self.guild_id, user_id,
            ))
            rows = await conn.fetch(
                """SELECT e.event_key,e.title,e.description,e.discount_amount,e.valid_days,e.created_at,
                          e.minimum_tier,e.purchaser_requirement,e.max_claims,e.min_order_amount,
                          (c.user_id IS NOT NULL) claimed,
                          (SELECT COUNT(*) FROM web_coupon_claims cc
                           WHERE cc.guild_id=e.guild_id AND cc.event_key=e.event_key)::INTEGER claim_count
                   FROM web_coupon_events e
                   LEFT JOIN web_coupon_claims c ON c.guild_id=e.guild_id AND c.event_key=e.event_key AND c.user_id=$2
                   WHERE e.guild_id=$1 AND e.is_active=TRUE ORDER BY e.created_at DESC""",
                self.guild_id, user_id,
            )
            welcome_claimed = bool(await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM web_coupon_claims WHERE guild_id=$1 AND event_key='WELCOME' AND user_id=$2)",
                self.guild_id, user_id,
            ))
        events = []
        for row in rows:
            item = dict(row)
            minimum_tier = item.get("minimum_tier") if item.get("minimum_tier") in COUPON_TIER_THRESHOLDS else "NONE"
            requirement = item.get("purchaser_requirement") or "ALL"
            reason = ""
            if int(item.get("max_claims") or 0) > 0 and int(item.get("claim_count") or 0) >= int(item["max_claims"]):
                reason = "쿠폰 수령이 마감되었습니다."
            elif total_spent < COUPON_TIER_THRESHOLDS[minimum_tier]:
                reason = f"{COUPON_TIER_LABELS[minimum_tier]} 등급 이상만 받을 수 있습니다."
            elif requirement == "FIRST_PURCHASE" and purchased:
                reason = "구매 이력이 없는 회원만 받을 수 있습니다."
            elif requirement == "EXISTING_BUYER" and not purchased:
                reason = "구매 이력이 있는 회원만 받을 수 있습니다."
            item.update({
                "discount_amount": int(item["discount_amount"]),
                "minimum_tier_label": COUPON_TIER_LABELS[minimum_tier],
                "eligible": not reason,
                "eligibility_reason": reason,
                "image_url": f"/api/coupon-events/{row['event_key']}/image",
            })
            events.append(item)
        if not purchased:
            events.insert(0, {
                "event_key": "WELCOME", "title": "웰컴 2,000원 할인쿠폰",
                "description": "V4P3 SP0T 첫 구매에 사용할 수 있는 신규 회원 전용 쿠폰입니다.",
                "discount_amount": 2_000, "valid_days": 30,
                "minimum_tier": "NONE", "minimum_tier_label": "등급 제한 없음",
                "purchaser_requirement": "FIRST_PURCHASE", "max_claims": 0, "claim_count": 0,
                "min_order_amount": 0, "eligible": True, "eligibility_reason": "",
                "claimed": welcome_claimed, "image_url": "/static/welcome-coupon.png", "created_at": "",
            })
        return web.json_response({
            "events": events,
            "welcome_pending": not purchased and not welcome_claimed,
        }, dumps=lambda value: json.dumps(value, ensure_ascii=False, default=str))

    async def claim_coupon_event(self, request):
        user = await self._user(request)
        user_id = int(user["id"])
        event_key = str(request.match_info["event_key"] or "").strip()
        async with self.bot.db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                    f"coupon-event:{self.guild_id}:{event_key}",
                )
                already = await conn.fetchval(
                    "SELECT 1 FROM web_coupon_claims WHERE guild_id=$1 AND event_key=$2 AND user_id=$3 FOR UPDATE",
                    self.guild_id, event_key, user_id,
                )
                if already:
                    raise StoreError("이미 받은 쿠폰입니다.", 409)
                if event_key == "WELCOME":
                    purchased = await conn.fetchval(
                        """SELECT EXISTS(SELECT 1 FROM orders WHERE guild_id=$1 AND buyer_id=$2 AND status='APPROVED')
                                  OR COALESCE((SELECT total_spent > 0 FROM user_info WHERE user_id=$2),FALSE)""",
                        self.guild_id, user_id,
                    )
                    if purchased:
                        raise StoreError("첫 구매 이력이 없는 사용자만 받을 수 있는 쿠폰입니다.", 403)
                    amount, days, min_order_amount = 2_000, 30, 0
                else:
                    event = await conn.fetchrow(
                        """SELECT discount_amount,valid_days,minimum_tier,purchaser_requirement,
                                  max_claims,min_order_amount FROM web_coupon_events
                           WHERE guild_id=$1 AND event_key=$2 AND is_active=TRUE FOR UPDATE""",
                        self.guild_id, event_key,
                    )
                    if not event:
                        raise StoreError("종료되었거나 존재하지 않는 쿠폰 이벤트입니다.", 404)
                    total_spent = int(await conn.fetchval(
                        "SELECT COALESCE(total_spent,0) FROM user_info WHERE user_id=$1", user_id,
                    ) or 0)
                    purchased = bool(await conn.fetchval(
                        """SELECT EXISTS(SELECT 1 FROM orders WHERE guild_id=$1 AND buyer_id=$2 AND status='APPROVED')
                                  OR COALESCE((SELECT total_spent > 0 FROM user_info WHERE user_id=$2),FALSE)""",
                        self.guild_id, user_id,
                    ))
                    minimum_tier = event["minimum_tier"] if event["minimum_tier"] in COUPON_TIER_THRESHOLDS else "NONE"
                    requirement = event["purchaser_requirement"] or "ALL"
                    claim_count = int(await conn.fetchval(
                        "SELECT COUNT(*) FROM web_coupon_claims WHERE guild_id=$1 AND event_key=$2",
                        self.guild_id, event_key,
                    ) or 0)
                    if int(event["max_claims"] or 0) > 0 and claim_count >= int(event["max_claims"]):
                        raise StoreError("쿠폰 수령이 마감되었습니다.", 409)
                    if total_spent < COUPON_TIER_THRESHOLDS[minimum_tier]:
                        raise StoreError(f"{COUPON_TIER_LABELS[minimum_tier]} 등급 이상만 받을 수 있습니다.", 403)
                    if requirement == "FIRST_PURCHASE" and purchased:
                        raise StoreError("구매 이력이 없는 회원만 받을 수 있습니다.", 403)
                    if requirement == "EXISTING_BUYER" and not purchased:
                        raise StoreError("구매 이력이 있는 회원만 받을 수 있습니다.", 403)
                    amount, days = int(event["discount_amount"]), int(event["valid_days"])
                    min_order_amount = int(event["min_order_amount"] or 0)
                code = f"V4P3-{secrets.token_hex(4).upper()}"
                coupon_id = await conn.fetchval(
                    """INSERT INTO web_coupons (guild_id,code,user_id,discount_amount,expires_at,min_order_amount)
                       VALUES ($1,$2,$3,$4,$5,$6) RETURNING id""",
                    self.guild_id, code, user_id, amount,
                    datetime.now().astimezone() + timedelta(days=days),
                    min_order_amount,
                )
                await conn.execute(
                    "INSERT INTO web_coupon_claims (guild_id,event_key,user_id,coupon_id) VALUES ($1,$2,$3,$4)",
                    self.guild_id, event_key, user_id, coupon_id,
                )
        return web.json_response({"ok": True, "code": code, "discount_amount": amount})

    async def coupon_event_image(self, request):
        event_key = str(request.match_info["event_key"] or "")
        async with self.bot.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT image_mime,image_data FROM web_coupon_events WHERE guild_id=$1 AND event_key=$2 AND is_active=TRUE",
                self.guild_id, event_key,
            )
        if not row:
            raise web.HTTPNotFound(text="쿠폰 이미지를 찾을 수 없습니다.")
        return web.Response(
            body=bytes(row["image_data"]), content_type=row["image_mime"],
            headers={"Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff"},
        )

    async def admin_create_coupon_event(self, request):
        await self._admin(request); data = await self._body(request)
        title = str(data.get("title") or "").strip()
        description = str(data.get("description") or "").strip()
        try:
            amount = int(data.get("discount_amount") or 0)
            days = int(data.get("valid_days") or 30)
            max_claims = int(data.get("max_claims") or 0)
            min_order_amount = int(data.get("min_order_amount") or 0)
        except (TypeError, ValueError) as exc:
            raise StoreError("쿠폰 금액, 사용 기한, 인원 제한과 최소 주문금액을 숫자로 입력해 주세요.") from exc
        minimum_tier = str(data.get("minimum_tier") or "NONE").upper()
        purchaser_requirement = str(data.get("purchaser_requirement") or "ALL").upper()
        if minimum_tier not in COUPON_TIER_THRESHOLDS:
            raise StoreError("쿠폰 등급 제한이 올바르지 않습니다.")
        if purchaser_requirement not in {"ALL", "FIRST_PURCHASE", "EXISTING_BUYER"}:
            raise StoreError("쿠폰 구매 이력 제한이 올바르지 않습니다.")
        if len(title) < 2 or amount <= 0 or not 1 <= days <= 365 or not 0 <= max_claims <= 1_000_000 or min_order_amount < 0:
            raise StoreError("쿠폰명, 금액, 사용 기한 및 제한 조건을 올바르게 입력해 주세요.")
        image_value = str(data.get("image_data_url") or "")
        match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64,(.+)", image_value, re.DOTALL)
        if not match:
            raise StoreError("PNG, JPG 또는 WEBP 쿠폰 이미지를 업로드해 주세요.")
        try:
            image_data = base64.b64decode(match.group(2), validate=True)
        except (ValueError, TypeError) as exc:
            raise StoreError("쿠폰 이미지 데이터를 읽을 수 없습니다.") from exc
        if not image_data or len(image_data) > 5 * 1024 * 1024:
            raise StoreError("쿠폰 이미지는 5MB 이하로 업로드해 주세요.")
        event_key = secrets.token_hex(8).upper()
        async with self.bot.db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO web_coupon_events
                   (guild_id,event_key,title,description,discount_amount,valid_days,minimum_tier,
                    purchaser_requirement,max_claims,min_order_amount,image_mime,image_data)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)""",
                self.guild_id, event_key, title, description, amount, days, minimum_tier,
                purchaser_requirement, max_claims, min_order_amount, match.group(1), image_data,
            )
        return web.json_response({"ok": True, "event_key": event_key})

    async def admin_coupon_events(self, request):
        await self._admin(request)
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT e.event_key,e.title,e.description,e.discount_amount,e.valid_days,e.created_at,
                          e.minimum_tier,e.purchaser_requirement,e.max_claims,e.min_order_amount,
                          COUNT(c.user_id)::INTEGER claim_count
                   FROM web_coupon_events e
                   LEFT JOIN web_coupon_claims c
                     ON c.guild_id=e.guild_id AND c.event_key=e.event_key
                   WHERE e.guild_id=$1
                   GROUP BY e.guild_id,e.event_key,e.title,e.description,e.discount_amount,e.valid_days,e.created_at,
                            e.minimum_tier,e.purchaser_requirement,e.max_claims,e.min_order_amount
                   ORDER BY e.created_at DESC""",
                self.guild_id,
            )
        return web.json_response(
            {"events": [dict(row) for row in rows]},
            dumps=lambda value: json.dumps(value, ensure_ascii=False, default=str),
        )

    async def admin_delete_coupon_event(self, request):
        await self._admin(request)
        event_key = str(request.match_info["event_key"] or "").strip()
        async with self.bot.db_pool.acquire() as conn:
            deleted = await conn.fetchval(
                "DELETE FROM web_coupon_events WHERE guild_id=$1 AND event_key=$2 RETURNING event_key",
                self.guild_id, event_key,
            )
        if not deleted:
            raise StoreError("이미 삭제되었거나 존재하지 않는 이벤트 쿠폰입니다.", 404)
        return web.json_response({"ok": True})

    async def cart(self, request):
        user = await self._user(request)
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.product_id, c.quantity, c.selected_option, c.selected_options_json,
                       p.name, p.price, p.image_url, p.option_label, p.options, p.option_groups_json,
                       p.free_shipping_quantity,
                       p.is_active, cat.name AS category_name
                FROM web_cart_items c JOIN web_products p ON p.id = c.product_id
                LEFT JOIN web_categories cat ON cat.id = p.category_id
                WHERE c.guild_id = $1 AND c.user_id = $2 ORDER BY c.created_at
                """, self.guild_id, int(user["id"]),
            )
        return web.json_response({"items": [dict(row) for row in rows]})

    async def save_cart(self, request):
        user, data = await self._user(request), await self._body(request)
        product_id, quantity = int(data.get("product_id", 0)), int(data.get("quantity", 1))
        raw_selected_options = data.get("selected_options", [])
        if not 1 <= quantity <= 99:
            raise StoreError("수량은 1~99개로 입력해 주세요.")
        async with self.bot.db_pool.acquire() as conn:
            product = await conn.fetchrow(
                "SELECT is_active, option_label, options, option_groups_json FROM web_products WHERE id = $1 AND guild_id = $2",
                product_id, self.guild_id,
            )
            if not product or not product["is_active"]:
                raise StoreError("판매 중인 상품이 아닙니다.", 404)
            groups = normalize_option_groups(
                product["option_groups_json"], product["option_label"], list(product["options"] or []),
            )
            selected_options = normalize_selected_options(groups, raw_selected_options)
            selected_option = selected_options[0] if selected_options else ""
            await conn.execute(
                """
                INSERT INTO web_cart_items
                    (guild_id, user_id, product_id, quantity, selected_option, selected_options_json)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (guild_id, user_id, product_id) DO UPDATE SET
                    quantity = EXCLUDED.quantity, selected_option = EXCLUDED.selected_option,
                    selected_options_json = EXCLUDED.selected_options_json
                """, self.guild_id, int(user["id"]), product_id, quantity, selected_option,
                json.dumps(selected_options, ensure_ascii=False),
            )
        return web.json_response({"ok": True})

    async def delete_cart(self, request):
        user = await self._user(request)
        async with self.bot.db_pool.acquire() as conn:
            await conn.execute("DELETE FROM web_cart_items WHERE guild_id = $1 AND user_id = $2 AND product_id = $3", self.guild_id, int(user["id"]), int(request.match_info["product_id"]))
        return web.json_response({"ok": True})

    async def create_web_ticket(self, request):
        user, data = await self._user(request), await self._body(request)
        type_labels = {"general": "일반문의", "payment": "입금문의", "shipping": "배송문의", "partner": "제휴문의"}
        ticket_type = str(data.get("ticket_type") or "general").strip().lower()
        if ticket_type not in type_labels:
            raise StoreError("문의 유형을 선택해 주세요.")
        content = str(data.get("문의내용") or data.get("content") or "").strip()
        extras = [f"{key}: {str(value).strip()}" for key, value in data.items() if key not in {"ticket_type", "문의내용", "content"} and str(value).strip()]
        if extras:
            content = "\n".join(extras + ([content] if content else []))
        if len(content) < 2 or len(content) > 3000:
            raise StoreError("문의사항을 2자 이상 3,000자 이하로 입력해 주세요.")
        async with self.bot.db_pool.acquire() as conn:
            inquiry_id = await conn.fetchval(
                "INSERT INTO web_inquiries (guild_id,user_id,inquiry_type,content) VALUES ($1,$2,$3,$4) RETURNING id",
                self.guild_id, int(user["id"]), ticket_type, content,
            )
            channel_id = await conn.fetchval("SELECT inquiry_alert_channel_id FROM guild_settings WHERE guild_id=$1", self.guild_id)
        if channel_id:
            origin = self.public_url or f"{request.scheme}://{request.host}"
            admin_url = f"{origin.rstrip('/')}/?admin_section=inquiries#admin"
            payload = {"flags": 32768, "components": [{"type": 17, "accent_color": 0x2ECC71, "components": [
                {"type": 10, "content": f"## 📩 새 {type_labels[ticket_type]}\n\n**작성자:** <@{user['id']}> (`{user['id']}`)\n**문의번호:** `{inquiry_id}`\n\n{discord.utils.escape_markdown(content)}"},
                {"type": 1, "components": [{"type": 2, "style": 5, "label": "답변하기", "url": admin_url}]},
            ]}], "allowed_mentions": {"parse": [], "users": [str(user["id"])]}}
            try:
                await self.bot.http.request(discord.http.Route("POST", f"/channels/{int(channel_id)}/messages"), json=payload)
            except Exception:
                LOGGER.exception("웹 문의 알림 전송 실패: inquiry=%s", inquiry_id)
        return web.json_response({"ok": True, "inquiry_id": str(inquiry_id)})

    async def inquiries(self, request):
        user = await self._user(request)
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT id,inquiry_type,content,status,answer,created_at,answered_at FROM web_inquiries WHERE guild_id=$1 AND user_id=$2 ORDER BY created_at DESC", self.guild_id, int(user["id"]))
        return web.json_response({"inquiries": [dict(row) for row in rows]}, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=str))

    async def admin_inquiries(self, request):
        await self._admin(request)
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM web_inquiries WHERE guild_id=$1 ORDER BY created_at DESC LIMIT 300", self.guild_id)
        result = []
        for row in rows:
            profile = await self._blacklist_profile(int(row["user_id"]))
            result.append({**dict(row), **profile, "user_id": str(row["user_id"])})
        return web.json_response({"inquiries": result}, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=str))

    async def admin_answer_inquiry(self, request):
        admin, data = await self._admin(request), await self._body(request)
        answer = str(data.get("answer") or "").strip()
        if len(answer) < 2 or len(answer) > 3000:
            raise StoreError("답변을 2자 이상 3,000자 이하로 입력해 주세요.")
        inquiry_id = int(request.match_info["inquiry_id"])
        async with self.bot.db_pool.acquire() as conn:
            row = await conn.fetchrow("UPDATE web_inquiries SET answer=$3,status='ANSWERED',answered_by=$4,answered_at=CURRENT_TIMESTAMP WHERE id=$1 AND guild_id=$2 RETURNING user_id,content", inquiry_id, self.guild_id, answer, int(admin["id"]))
        if not row:
            raise StoreError("문의를 찾을 수 없습니다.", 404)
        try:
            target = self.bot.get_user(int(row["user_id"])) or await self.bot.fetch_user(int(row["user_id"]))
            dm = await target.create_dm()
            payload = {
                "flags": 32768,
                "components": [{
                    "type": 17,
                    "accent_color": 0x5865F2,
                    "components": [{
                        "type": 10,
                        "content": (
                            "## 📩 V4P3 SP0T 문의 답변\n\n"
                            f"{discord.utils.escape_markdown(answer)}\n\n"
                            "-# 언제나 베이프가 필요할 땐? V4P3 SP0T"
                        ),
                    }],
                }],
                "allowed_mentions": {"parse": []},
            }
            await self.bot.http.request(
                discord.http.Route("POST", f"/channels/{dm.id}/messages"),
                json=payload,
            )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass
        return web.json_response({"ok": True})

    async def admin_delete_inquiry(self, request):
        await self._admin(request)
        inquiry_id = int(request.match_info["inquiry_id"])
        async with self.bot.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT id,user_id,inquiry_type,content,status,answer,answered_by,created_at,answered_at
                   FROM web_inquiries WHERE id=$1 AND guild_id=$2""",
                inquiry_id, self.guild_id,
            )
        if not row:
            raise StoreError("문의를 찾을 수 없습니다.", 404)
        if row["status"] != "ANSWERED" or not str(row["answer"] or "").strip():
            raise StoreError("답변이 완료된 문의만 삭제할 수 있습니다.")
        archive_text = (
            "V4P3 SP0T 문의 기록\n"
            "=" * 48 + "\n"
            f"문의번호: {row['id']}\n"
            f"작성자 Discord ID: {row['user_id']}\n"
            f"문의유형: {row['inquiry_type']}\n"
            f"문의등록: {row['created_at']}\n"
            f"답변관리자 ID: {row['answered_by'] or '알 수 없음'}\n"
            f"답변완료: {row['answered_at'] or '알 수 없음'}\n\n"
            "[문의사항]\n"
            f"{row['content']}\n\n"
            "[문의답변]\n"
            f"{row['answer']}\n"
        )
        try:
            admin_user = self.bot.get_user(ADMIN_USER_ID) or await self.bot.fetch_user(ADMIN_USER_ID)
            archive_file = discord.File(
                BytesIO(archive_text.encode("utf-8-sig")),
                filename=f"V4P3-SP0T-inquiry-{inquiry_id}.txt",
            )
            await admin_user.send(
                f"문의 #{inquiry_id} 삭제 전 기록입니다.",
                file=archive_file,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            raise StoreError("관리자 DM으로 문의 기록 파일을 보내지 못해 문의를 삭제하지 않았습니다.", 503) from exc
        async with self.bot.db_pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM web_inquiries WHERE id=$1 AND guild_id=$2 AND status='ANSWERED'",
                inquiry_id, self.guild_id,
            )
        if result == "DELETE 0":
            raise StoreError("문의 상태가 변경되어 삭제하지 못했습니다.", 409)
        return web.json_response({"ok": True})

    async def admin_direct_dm(self, request):
        await self._admin(request)
        raw_user_id = str(request.query.get("user_id") or "").strip()
        selected_user_id = self._parse_discord_user_id(raw_user_id) if raw_user_id else None
        async with self.bot.db_pool.acquire() as conn:
            sessions = await conn.fetch(
                "SELECT user_id,created_at,updated_at FROM web_direct_dm_sessions WHERE guild_id=$1 ORDER BY updated_at DESC LIMIT 100",
                self.guild_id,
            )
            messages = await conn.fetch(
                "SELECT id,sender_type,sender_name,content,created_at FROM web_direct_dm_messages WHERE guild_id=$1 AND user_id=$2 ORDER BY id DESC LIMIT 200",
                self.guild_id, selected_user_id,
            ) if selected_user_id else []
        session_items = []
        for row in sessions:
            profile = await self._blacklist_profile(int(row["user_id"]))
            session_items.append({**dict(row), **profile, "user_id": str(row["user_id"])})
        selected_profile = await self._blacklist_profile(selected_user_id) if selected_user_id else None
        return web.json_response(
            {"sessions": session_items, "selected_user": ({**selected_profile, "user_id": str(selected_user_id)} if selected_user_id else None), "messages": [dict(row) for row in reversed(messages)]},
            dumps=lambda value: json.dumps(value, ensure_ascii=False, default=str),
        )

    async def admin_send_direct_dm(self, request):
        admin, data = await self._admin(request), await self._body(request)
        user_id = self._parse_discord_user_id(data.get("user_id"))
        content = str(data.get("content") or "").strip()
        if not 1 <= len(content) <= 2000:
            raise StoreError("DM 내용은 1자 이상 2,000자 이하로 입력해 주세요.")
        try:
            target = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            dm = await target.create_dm()
            payload = {
                "flags": 1 << 15,
                "components": [{"type": 17, "accent_color": 0x5865F2, "components": [{"type": 10, "content": f"## V4P3 SP0T\n\n{discord.utils.escape_markdown(content)}"}]}],
                "allowed_mentions": {"parse": []},
            }
            await self.bot.http.request(discord.http.Route("POST", f"/channels/{dm.id}/messages"), json=payload)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            raise StoreError("해당 유저에게 DM을 보낼 수 없습니다. 유저 ID와 DM 허용 설정을 확인해 주세요.", 422) from exc
        async with self.bot.db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO web_direct_dm_sessions (guild_id,user_id) VALUES ($1,$2) ON CONFLICT (guild_id,user_id) DO UPDATE SET updated_at=CURRENT_TIMESTAMP",
                self.guild_id, user_id,
            )
            await conn.execute(
                "INSERT INTO web_direct_dm_messages (guild_id,user_id,sender_type,sender_name,content) VALUES ($1,$2,'ADMIN',$3,$4)",
                self.guild_id, user_id, str(admin.get("username") or "관리자"), content,
            )
        return web.json_response({"ok": True})

    async def _chat_availability(self, conn) -> dict:
        row = await conn.fetchrow("SELECT live_chat_start,live_chat_end FROM guild_settings WHERE guild_id=$1", self.guild_id)
        start = str(row["live_chat_start"] if row else "10:00")
        end = str(row["live_chat_end"] if row else "22:00")
        try:
            now = datetime.now(timezone(timedelta(hours=9))).strftime("%H:%M")
            available = start <= now < end if start <= end else now >= start or now < end
        except Exception:
            available = True
        return {"available": available, "start": start, "end": end}

    @staticmethod
    def _chat_session_dict(row) -> dict:
        item = dict(row)
        for key in ("id", "user_id", "thread_id", "assigned_to"):
            if item.get(key) is not None:
                item[key] = str(item[key])
        return item

    async def chat_sessions(self, request):
        user = await self._user(request)
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM web_chat_sessions WHERE guild_id=$1 AND user_id=$2 ORDER BY updated_at DESC LIMIT 30", self.guild_id, int(user["id"]))
            availability = await self._chat_availability(conn)
        return web.json_response({"sessions": [self._chat_session_dict(row) for row in rows], **availability}, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=str))

    async def create_chat_session(self, request):
        user, data = await self._user(request), await self._body(request)
        subject = str(data.get("content") or "").strip()
        if len(subject) < 2 or len(subject) > 1000:
            raise StoreError("문의 내용을 2자 이상 1,000자 이하로 입력해 주세요.")
        guild = self._guild()
        if guild is None:
            raise StoreError("Discord 서버를 찾을 수 없습니다.", 503)
        async with self.bot.db_pool.acquire() as conn:
            availability = await self._chat_availability(conn)
            if not availability["available"]:
                raise StoreError(f"현재 상담시간이 아닙니다. 상담시간은 {availability['start']}~{availability['end']}입니다.")
            existing = await conn.fetchrow("SELECT * FROM web_chat_sessions WHERE guild_id=$1 AND user_id=$2 AND status IN ('OPEN','ASSIGNED','CLOSING') ORDER BY created_at DESC LIMIT 1", self.guild_id, int(user["id"]))
            if existing:
                return web.json_response({"session": self._chat_session_dict(existing), "existing": True}, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=str))
            channel_id = await conn.fetchval("SELECT live_chat_channel_id FROM guild_settings WHERE guild_id=$1", self.guild_id)
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            raise StoreError("실시간 상담 Discord 채널이 아직 설정되지 않았습니다.", 503)
        safe_name = re.sub(r"[^0-9A-Za-z가-힣_-]+", "-", str(user.get("username") or user["id"])).strip("-")
        try:
            thread = await channel.create_thread(name=f"{safe_name or user['id']}-{user['id']}"[:100], type=discord.ChannelType.public_thread, reason="웹 실시간 상담")
        except (discord.Forbidden, discord.HTTPException) as exc:
            raise StoreError("상담 스레드를 만들지 못했습니다. 봇 권한을 확인해 주세요.", 503) from exc
        async with self.bot.db_pool.acquire() as conn:
            session = await conn.fetchrow("INSERT INTO web_chat_sessions (guild_id,user_id,thread_id,subject) VALUES ($1,$2,$3,$4) RETURNING *", self.guild_id, int(user["id"]), thread.id, subject)
            await conn.execute("INSERT INTO web_chat_messages (session_id,sender_type,sender_id,sender_name,content) VALUES ($1,'USER',$2,$3,$4)", session["id"], int(user["id"]), str(user.get("username") or user["id"]), subject)
        payload = {"flags": 32768, "components": [{"type": 17, "accent_color": 0x3498DB, "components": [
            {"type": 10, "content": f"## 💬 웹 실시간 상담 #{session['id']}\n\n**사용자:** <@{user['id']}> (`{user['id']}`)\n**문의내용:**\n{discord.utils.escape_markdown(subject)}"},
            {"type": 1, "components": [{"type": 2, "style": 1, "label": "문의 담당", "custom_id": f"web_chat_assign:{session['id']}"}]},
        ]}], "allowed_mentions": {"parse": [], "users": [str(user["id"])]}}
        await self.bot.http.request(discord.http.Route("POST", f"/channels/{thread.id}/messages"), json=payload)
        return web.json_response({"session": self._chat_session_dict(session)}, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=str))

    async def chat_session(self, request):
        user = await self._user(request)
        session_id = int(request.match_info["session_id"])
        async with self.bot.db_pool.acquire() as conn:
            session = await conn.fetchrow("SELECT * FROM web_chat_sessions WHERE id=$1 AND guild_id=$2 AND user_id=$3", session_id, self.guild_id, int(user["id"]))
            if not session:
                raise StoreError("상담 기록을 찾을 수 없습니다.", 404)
            rows = await conn.fetch("SELECT id,sender_type,sender_name,content,(image_data IS NOT NULL) has_image,created_at FROM web_chat_messages WHERE session_id=$1 ORDER BY id", session_id)
        messages = [{**dict(row), "id": str(row["id"]), "image_url": f"/api/chat/messages/{row['id']}/image" if row["has_image"] else ""} for row in rows]
        return web.json_response({"session": self._chat_session_dict(session), "messages": messages}, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=str))

    async def chat_message(self, request):
        user = await self._user(request)
        session_id = int(request.match_info["session_id"])
        content, image_data, image_mime = "", None, None
        if request.content_type.startswith("multipart/"):
            reader = await request.multipart()
            async for part in reader:
                if part.name == "content": content = (await part.text()).strip()
                elif part.name == "image" and part.filename:
                    image_data = await part.read(decode=False); image_mime = part.headers.get("Content-Type", "")
        else:
            content = str((await self._body(request)).get("content") or "").strip()
        if not content and not image_data:
            raise StoreError("메시지나 이미지를 입력해 주세요.")
        if len(content) > 2000 or (image_data and len(image_data) > 5 * 1024 * 1024):
            raise StoreError("메시지는 2,000자, 이미지는 5MB 이하로 보내 주세요.")
        if image_data and image_mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            raise StoreError("PNG, JPG, WEBP 또는 GIF 이미지만 보낼 수 있습니다.")
        async with self.bot.db_pool.acquire() as conn:
            session = await conn.fetchrow("SELECT * FROM web_chat_sessions WHERE id=$1 AND guild_id=$2 AND user_id=$3", session_id, self.guild_id, int(user["id"]))
            if not session or session["status"] not in {"OPEN", "ASSIGNED", "CLOSING"}:
                raise StoreError("종료된 상담에는 메시지를 보낼 수 없습니다.")
            message_id = await conn.fetchval("INSERT INTO web_chat_messages (session_id,sender_type,sender_id,sender_name,content,image_mime,image_data) VALUES ($1,'USER',$2,$3,$4,$5,$6) RETURNING id", session_id, int(user["id"]), str(user.get("username") or user["id"]), content, image_mime, image_data)
            await conn.execute("UPDATE web_chat_sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=$1", session_id)
        thread = self.bot.get_channel(int(session["thread_id"]))
        if thread:
            file = discord.File(BytesIO(image_data), filename=f"chat-{message_id}.{(image_mime or 'image/png').split('/')[-1]}") if image_data else None
            await thread.send(content or "이미지를 보냈습니다.", file=file, allowed_mentions=discord.AllowedMentions.none())
        return web.json_response({"ok": True, "message_id": str(message_id)})

    async def chat_message_image(self, request):
        user = await self._user(request)
        async with self.bot.db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT m.image_mime,m.image_data FROM web_chat_messages m JOIN web_chat_sessions s ON s.id=m.session_id WHERE m.id=$1 AND s.guild_id=$2 AND s.user_id=$3", int(request.match_info["message_id"]), self.guild_id, int(user["id"]))
        if not row or not row["image_data"]:
            raise StoreError("이미지를 찾을 수 없습니다.", 404)
        return web.Response(body=bytes(row["image_data"]), content_type=row["image_mime"] or "image/png")

    async def chat_close_choice(self, request):
        user, data = await self._user(request), await self._body(request)
        session_id = int(request.match_info["session_id"]); choice = str(data.get("choice") or "")
        if choice not in {"close", "continue"}:
            raise StoreError("종료 또는 계속하기를 선택해 주세요.")
        status, text = ("CLOSED", "상담이 종료되었습니다!") if choice == "close" else ("ASSIGNED", "문의가 계속됩니다!")
        async with self.bot.db_pool.acquire() as conn:
            session = await conn.fetchrow("UPDATE web_chat_sessions SET status=$4,updated_at=CURRENT_TIMESTAMP,closed_at=CASE WHEN $4='CLOSED' THEN CURRENT_TIMESTAMP ELSE NULL END WHERE id=$1 AND guild_id=$2 AND user_id=$3 AND status='CLOSING' RETURNING *", session_id, self.guild_id, int(user["id"]), status)
            if not session: raise StoreError("처리할 종료 요청이 없습니다.")
            await conn.execute("INSERT INTO web_chat_messages (session_id,sender_type,sender_name,content) VALUES ($1,'SYSTEM','V4P3 SP0T',$2)", session_id, text)
        thread = self.bot.get_channel(int(session["thread_id"]))
        if thread: await thread.send(f"**{user.get('username', user['id'])}**님이 상담을 {'종료했습니다.' if choice == 'close' else '계속하기로 했습니다.'}")
        return web.json_response({"ok": True, "status": status})

    async def handle_discord_chat_interaction(self, interaction) -> bool:
        custom_id = str((interaction.data or {}).get("custom_id") or "")
        if not custom_id.startswith("web_chat_assign:"):
            return False
        session_id = int(custom_id.split(":", 1)[1])
        async with self.bot.db_pool.acquire() as conn:
            row = await conn.fetchrow("UPDATE web_chat_sessions SET status='ASSIGNED',assigned_to=$2,updated_at=CURRENT_TIMESTAMP WHERE id=$1 AND status='OPEN' RETURNING id", session_id, interaction.user.id)
            if row:
                await conn.execute("INSERT INTO web_chat_messages (session_id,sender_type,sender_id,sender_name,content) VALUES ($1,'SYSTEM',$2,'V4P3 SP0T','상담사가 연결되었습니다!')", session_id, interaction.user.id)
        await interaction.response.send_message("✅ 상담 담당자로 연결되었습니다." if row else "이미 담당자가 연결되었거나 종료된 상담입니다.", ephemeral=True)
        return True

    async def handle_discord_chat_message(self, message) -> bool:
        if message.author.bot:
            return False
        if message.guild is None:
            content_parts = [str(message.content or "").strip()]
            content_parts.extend(str(attachment.url) for attachment in message.attachments)
            content = "\n".join(part for part in content_parts if part).strip()
            if not content:
                return False
            async with self.bot.db_pool.acquire() as conn:
                exists = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM web_direct_dm_sessions WHERE guild_id=$1 AND user_id=$2)",
                    self.guild_id, message.author.id,
                )
                if not exists:
                    return False
                await conn.execute(
                    "INSERT INTO web_direct_dm_messages (guild_id,user_id,sender_type,sender_name,content) VALUES ($1,$2,'USER',$3,$4)",
                    self.guild_id, message.author.id, str(message.author.display_name), content,
                )
                await conn.execute(
                    "UPDATE web_direct_dm_sessions SET updated_at=CURRENT_TIMESTAMP WHERE guild_id=$1 AND user_id=$2",
                    self.guild_id, message.author.id,
                )
            return True
        if not isinstance(message.channel, discord.Thread):
            return False
        async with self.bot.db_pool.acquire() as conn:
            session = await conn.fetchrow("SELECT * FROM web_chat_sessions WHERE thread_id=$1", message.channel.id)
            if not session:
                return False
            content = str(message.content or "").strip()
            if content == "!상담종료":
                guild = self._guild()
                member = guild.get_member(int(session["user_id"])) if guild else None
                if member is None and guild:
                    try:
                        member = await guild.fetch_member(int(session["user_id"]))
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        member = None
                display_name = member.display_name if member else "고객"
                prompt = f"{display_name}님! 문의를 종료해도 될까요?"
                await conn.execute("UPDATE web_chat_sessions SET status='CLOSING',updated_at=CURRENT_TIMESTAMP WHERE id=$1", session["id"])
                await conn.execute("INSERT INTO web_chat_messages (session_id,sender_type,sender_name,content) VALUES ($1,'CLOSE_PROMPT','V4P3 SP0T',$2)", session["id"], prompt)
                await message.add_reaction("✅")
                return True
            if content.startswith("!상담강제종료"):
                reason = content.removeprefix("!상담강제종료").strip() or "관리자에 의해 종료됨"
                await conn.execute("UPDATE web_chat_sessions SET status='FORCE_CLOSED',closed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=$1", session["id"])
                await conn.execute("INSERT INTO web_chat_messages (session_id,sender_type,sender_name,content) VALUES ($1,'SYSTEM','V4P3 SP0T',$2)", session["id"], f"상담이 강제로 종료되었습니다. 사유: {reason}")
                await message.add_reaction("✅")
                return True
            if session["status"] not in {"OPEN", "ASSIGNED", "CLOSING"}:
                return True
            attachments = [attachment for attachment in message.attachments if (attachment.content_type or "").startswith("image/")]
            image_data = await attachments[0].read() if attachments and attachments[0].size <= 5 * 1024 * 1024 else None
            image_mime = attachments[0].content_type if image_data else None
            if content or image_data:
                await conn.execute("INSERT INTO web_chat_messages (session_id,sender_type,sender_id,sender_name,content,image_mime,image_data) VALUES ($1,'STAFF',$2,$3,$4,$5,$6)", session["id"], message.author.id, message.author.display_name, content, image_mime, image_data)
                await conn.execute("UPDATE web_chat_sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=$1", session["id"])
        return True

    async def checkout(self, request):
        user, data = await self._user(request), await self._body(request)
        async with self.bot.db_pool.acquire() as conn:
            verified_role_id = await conn.fetchval(
                "SELECT purchase_verified_role_id FROM guild_settings WHERE guild_id=$1",
                self.guild_id,
            )
            if verified_role_id:
                guild = self._guild()
                member = guild.get_member(int(user["id"])) if guild else None
                if member is None and guild:
                    try:
                        member = await guild.fetch_member(int(user["id"]))
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        member = None
                if member is None or all(role.id != int(verified_role_id) for role in member.roles):
                    raise StoreError("서버 인증 역할이 있는 유저만 구매할 수 있습니다. Discord 서버에서 인증을 완료해 주세요.", 403)
            blacklist_reason = await conn.fetchval(
                """SELECT reason FROM web_blacklist WHERE guild_id=$1 AND user_id=$2
                   AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP)""",
                self.guild_id, int(user["id"]),
            )
            registered = await conn.fetchrow(
                """SELECT name,contact,address,cvs,cvs_brand,cvs_address,refund_bank,
                          refund_account_number,refund_account_holder,refund_policy_version
                   FROM user_info WHERE user_id=$1""",
                int(user["id"]),
            )
            current_policy = await self._policy_data(conn)
        if blacklist_reason:
            raise StoreError(f"블랙리스트에 등록되어 주문할 수 없습니다. 사유: {blacklist_reason}", 403)
        if not registered or not registered["name"] or not registered["contact"] or not all((registered["refund_bank"], registered["refund_account_number"], registered["refund_account_holder"])):
            raise StoreError("정보등록에서 배송 정보와 환불계좌를 먼저 등록해 주세요.", 409)
        if registered["refund_policy_version"] != current_policy["refund_policy_version"]:
            raise StoreError("정보등록에서 최신 환불약관을 확인하고 다시 동의해 주세요.", 409)
        if not registered["address"] and not (registered["cvs"] and registered["cvs_brand"] and registered["cvs_address"]):
            raise StoreError("정보등록에서 집주소 또는 편의점 수령 정보를 먼저 등록해 주세요.", 409)
        if not data.get("order_confirmed"):
            raise StoreError("주문 내용이 정확한지 확인해 주세요.")
        depositor = str(data.get("depositor_name", "")).strip()
        if not depositor:
            raise StoreError("입금자명을 입력해 주세요.")
        shipping_labels = {
            "GENERAL": "일반택배",
            "GS25": "GS25반값택배",
            "CU": "CU알뜰택배",
        }
        shipping_method = str(data.get("shipping_method", "")).strip().upper()
        if shipping_method not in shipping_labels:
            raise StoreError("택배 방식을 선택해 주세요.")
        shipping_name = str(data.get("name", "")).strip()
        shipping_contact = str(data.get("contact", "")).strip()
        shipping_address = str(data.get("address", "")).strip()
        shipping_cvs = str(data.get("cvs", "")).strip()
        shipping_memo = str(data.get("shipping_memo", "")).strip()
        if len(shipping_memo) > 300:
            raise StoreError("배송메모는 300자 이하로 입력해 주세요.")
        if not shipping_name or not shipping_contact or not shipping_address:
            raise StoreError("받는 분의 이름, 연락처, 배송 주소를 모두 입력해 주세요.")
        if shipping_method in {"GS25", "CU"} and not shipping_cvs:
            raise StoreError("조회 결과에서 받을 편의점을 선택해 주세요.")
        if shipping_method == "GENERAL":
            shipping_cvs = ""
        try:
            points = int(data.get("points", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise StoreError("포인트는 숫자로 입력해 주세요.") from exc
        if points != 0 and not 500 <= points <= 2000:
            raise StoreError("포인트는 사용하지 않으려면 0P, 사용할 때는 500P~2,000P만 사용할 수 있습니다.")
        selected = [int(value) for value in data.get("product_ids", [])]
        if not selected:
            raise StoreError("구매할 상품을 선택해 주세요.")
        user_id = int(user["id"])
        order_id = f"WEB-{int(time.time()):X}-{secrets.token_hex(3).upper()}"
        cash_amount = None
        async with self.bot.db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO user_info (user_id, name, contact, address, cvs)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (user_id) DO UPDATE SET
                        name=EXCLUDED.name, contact=EXCLUDED.contact,
                        address=EXCLUDED.address, cvs=EXCLUDED.cvs
                    """, user_id, shipping_name, shipping_contact, shipping_address, shipping_cvs,
                )
                rows = await conn.fetch(
                    """
                    SELECT c.product_id, c.quantity, c.selected_option, c.selected_options_json,
                           p.name, p.price, p.is_active, p.option_label, p.options, p.option_groups_json,
                           p.free_shipping_quantity
                    FROM web_cart_items c JOIN web_products p ON p.id = c.product_id
                    WHERE c.guild_id = $1 AND c.user_id = $2 AND c.product_id = ANY($3::bigint[])
                    ORDER BY c.product_id FOR UPDATE OF p
                    """, self.guild_id, user_id, selected,
                )
                if len(rows) != len(set(selected)):
                    raise StoreError("장바구니 상품 일부를 찾을 수 없습니다.")
                if any(not row["is_active"] for row in rows):
                    raise StoreError("판매가 종료된 상품이 있습니다.")
                normalized_selections = {}
                normalized_groups = {}
                for row in rows:
                    groups = normalize_option_groups(
                        row["option_groups_json"], row["option_label"], list(row["options"] or []),
                    )
                    normalized_groups[row["product_id"]] = groups
                    try:
                        normalized_selections[row["product_id"]] = normalize_selected_options(
                            groups, row["selected_options_json"],
                        )
                    except StoreError as error:
                        raise StoreError(f"{row['name']} 상품: {error}") from error
                subtotal = sum(
                    (int(row["price"]) + product_selection_surcharge(normalized_groups[row["product_id"]], normalized_selections[row["product_id"]]))
                    * int(row["quantity"])
                    for row in rows
                )
                shipping_fee = 0 if qualifies_for_product_free_shipping(rows) else calculate_shipping_fee(subtotal)
                total = subtotal + shipping_fee
                coupon_code = str(data.get("coupon_code") or "").strip()
                coupon_discount = 0
                if coupon_code:
                    coupon = await conn.fetchrow(
                        """SELECT id,discount_amount,min_order_amount FROM web_coupons WHERE guild_id=$1 AND user_id=$2 AND code=$3
                           AND used_order_id IS NULL AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP) FOR UPDATE""",
                        self.guild_id, user_id, coupon_code,
                    )
                    if not coupon:
                        raise StoreError("사용할 수 없는 쿠폰입니다.")
                    if subtotal < int(coupon["min_order_amount"] or 0):
                        raise StoreError(f"이 쿠폰은 상품 금액 {int(coupon['min_order_amount']):,}원 이상 구매할 때 사용할 수 있습니다.")
                    coupon_discount = min(int(coupon["discount_amount"]), total)
                    total -= coupon_discount
                if not self.payment_service.enabled and total - points > 0:
                    raise StoreError("Pushbullet 자동결제가 아직 설정되지 않았습니다. 관리자에게 문의해 주세요.", 503)
                def describe_product(row):
                    selections = normalized_selections[row["product_id"]]
                    if not selections:
                        return row["name"]
                    details = " / ".join(
                        f"{group['label']}: {answer}"
                        for group, answer in zip(normalized_groups[row["product_id"]], selections)
                    )
                    return f"{row['name']} ({details})"

                product_text = ", ".join(describe_product(row) for row in rows)
                quantity_text = f"총 {sum(int(row['quantity']) for row in rows)}개"
                await conn.execute(
                    """
                    INSERT INTO orders (
                        order_id, guild_id, original_channel_id, buyer_id, product,
                        quantity, amount, status, points_allowed, source,
                        shipping_name, shipping_contact, shipping_address, shipping_cvs,
                        shipping_method, shipping_fee, coupon_code, coupon_discount, shipping_memo
                    ) VALUES ($1, $2, NULL, $3, $4, $5, $6, 'PENDING', TRUE, 'WEB',
                              $7, $8, $9, $10, $11, $12, $13, $14, $15)
                    """, order_id, self.guild_id, user_id, product_text, quantity_text, f"{total:,}원",
                    shipping_name, shipping_contact, shipping_address, shipping_cvs,
                    shipping_labels[shipping_method], shipping_fee, coupon_code or None, coupon_discount, shipping_memo,
                )
                if coupon_code:
                    await conn.execute("UPDATE web_coupons SET used_order_id=$2 WHERE guild_id=$1 AND code=$3", self.guild_id, order_id, coupon_code)
                for row in rows:
                    await conn.execute(
                        """
                        INSERT INTO web_order_items
                            (order_id, product_id, product_name, unit_price, quantity,
                             product_option, product_options_json)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """, order_id, row["product_id"], row["name"],
                        int(row["price"]) + product_selection_surcharge(normalized_groups[row["product_id"]], normalized_selections[row["product_id"]]),
                        row["quantity"],
                        normalized_selections[row["product_id"]][0] if normalized_selections[row["product_id"]] else "",
                        json.dumps(normalized_selections[row["product_id"]], ensure_ascii=False),
                    )
                order = await request_payment(conn, order_id, self.guild_id, user_id, depositor, points)
                order = await arm_web_payment(conn, order_id, self.guild_id)
                cash_amount = int(order["cash_amount"] or 0)
                if cash_amount > 0:
                    duplicates = await conn.fetchval(
                        """
                        SELECT COUNT(*) FROM orders
                        WHERE status = 'PENDING' AND source = 'WEB'
                          AND payment_deadline > CURRENT_TIMESTAMP
                          AND depositor_key = $1 AND cash_amount = $2
                        """, order["depositor_key"], cash_amount,
                    )
                    if duplicates > 1:
                        raise StoreError("같은 입금자명과 금액의 대기 주문이 있습니다. 기존 주문이 끝난 뒤 다시 신청해 주세요.")
                await conn.execute("DELETE FROM web_cart_items WHERE guild_id = $1 AND user_id = $2 AND product_id = ANY($3::bigint[])", self.guild_id, user_id, selected)

        await self._send_manual_payment_request(order)

        if cash_amount == 0:
            async with self.bot.db_pool.acquire() as conn:
                await conn.execute("UPDATE orders SET auto_payment_event_id = $2 WHERE order_id = $1", order_id, f"POINTS:{order_id}")
                approved = await resolve_payment(conn, order_id, self.guild_id, True)
            await finalize_approved_order(self.bot, approved, processor="포인트 전액결제")
            status = "APPROVED"
        else:
            status = "PENDING"
        async with self.bot.db_pool.acquire() as conn:
            settings = await conn.fetchrow("SELECT bank_name, account_number, account_holder FROM guild_settings WHERE guild_id = $1", self.guild_id)
        return web.json_response({
            "order_id": order_id,
            "status": status,
            "cash_amount": cash_amount,
            "shipping_fee": shipping_fee,
            "deadline_seconds": 300,
            "bank": dict(settings) if settings else {},
        })

    async def _send_manual_payment_request(self, order):
        async with self.bot.db_pool.acquire() as conn:
            channel_id = await conn.fetchval("SELECT approval_channel_id FROM guild_settings WHERE guild_id=$1", self.guild_id)
        if not channel_id:
            return
        content = (
            "## 🔔 새로운 웹 결제 확인 요청\n\n"
            f"`👤` **구매자**\n<@{order['buyer_id']}>\n\n"
            f"`✍️` **입금자명**\n`{order.get('depositor_name') or '미입력'}`\n\n"
            f"`◾` **주문번호**\n`{order['order_id']}`\n\n"
            f"`📦` **상품**\n`{order['product']}`\n\n"
            f"`💰` **입금해야 할 금액**\n**{int(order.get('cash_amount') or 0):,}원**"
        )
        payload = {
            "flags": 32768,
            "components": [{"type": 17, "accent_color": 0x28CF7C, "components": [
                {"type": 10, "content": content},
                {"type": 1, "components": [
                    {"type": 2, "style": 3, "label": "승인", "custom_id": f"approve_{order['order_id']}"},
                    {"type": 2, "style": 4, "label": "거절", "custom_id": f"reject_{order['order_id']}"},
                ]},
            ]}],
            "allowed_mentions": {"parse": []},
        }
        try:
            response = await self.bot.http.request(discord.http.Route("POST", f"/channels/{int(channel_id)}/messages"), json=payload)
            if response and response.get("id"):
                async with self.bot.db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE orders SET approval_channel_id=$3,approval_message_id=$4 WHERE order_id=$1 AND guild_id=$2",
                        order["order_id"], self.guild_id, int(channel_id), int(response["id"]),
                    )
        except discord.HTTPException:
            LOGGER.exception("웹 결제 확인 요청 전송 실패: order=%s", order["order_id"])

    async def orders(self, request):
        user = await self._user(request)
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT order_id, product, quantity, amount, status, points_used, cash_amount,
                       created_at, processed_at, payment_deadline, cancel_reason,
                       shipping_method, waybill_number, carrier_id,
                       fulfillment_status, fulfillment_updated_at
                FROM orders WHERE guild_id = $1 AND buyer_id = $2 AND source = 'WEB'
                ORDER BY created_at DESC LIMIT 100
                """, self.guild_id, int(user["id"]),
            )
        return web.json_response({"orders": [dict(row) for row in rows]}, dumps=lambda value: json.dumps(value, ensure_ascii=False, default=str))

    async def order_tracking(self, request):
        user = await self._user(request)
        async with self.bot.db_pool.acquire() as conn:
            order = await conn.fetchrow(
                """
                SELECT waybill_number, carrier_id
                FROM orders
                WHERE order_id=$1 AND guild_id=$2 AND buyer_id=$3 AND source='WEB'
                """,
                request.match_info["order_id"], self.guild_id, int(user["id"]),
            )
        if not order:
            raise StoreError("주문을 찾을 수 없습니다.", 404)
        if not order["waybill_number"]:
            raise StoreError("아직 운송장이 등록되지 않았습니다.")
        if not order["carrier_id"]:
            raise StoreError("택배사 정보가 없어 배송조회 메뉴에서 택배사를 직접 선택해 주세요.")
        try:
            payload = await self._tracking_payload(order["carrier_id"], order["waybill_number"])
        except DeliveryTrackingError as exc:
            raise StoreError(str(exc), 400) from exc
        return web.json_response(payload, dumps=lambda value: json.dumps(value, ensure_ascii=False))

    async def order(self, request):
        user = await self._user(request)
        async with self.bot.db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM orders WHERE order_id = $1 AND guild_id = $2 AND buyer_id = $3 AND source = 'WEB'", request.match_info["order_id"], self.guild_id, int(user["id"]))
        if not row:
            raise StoreError("주문을 찾을 수 없습니다.", 404)
        return web.json_response(dict(row), dumps=lambda value: json.dumps(value, ensure_ascii=False, default=str))

    @staticmethod
    def _parse_discord_user_id(value) -> int:
        text = str(value or "").strip()
        if not re.fullmatch(r"\d{15,20}", text):
            raise StoreError("Discord 개발자 모드에서 복사한 사용자 ID를 입력해 주세요.")
        return int(text)

    async def _blacklist_profile(self, user_id: int) -> dict:
        member = None
        guild = self._guild()
        if guild is not None:
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    member = None
        user = member or self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                user = None
        if user is None:
            return {
                "nickname": "확인할 수 없는 사용자",
                "username": "알 수 없음",
                "avatar_url": "",
            }
        nickname = getattr(member, "display_name", None) or getattr(user, "global_name", None) or user.name
        username = user.name
        discriminator = getattr(user, "discriminator", "0")
        if discriminator and discriminator != "0":
            username = f"{username}#{discriminator}"
        return {
            "nickname": nickname,
            "username": username,
            "avatar_url": str(user.display_avatar.url),
        }

    async def admin_blacklist(self, request):
        await self._admin(request)
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, reason, added_by, blacklist_type, expires_at, created_at
                FROM web_blacklist
                WHERE guild_id=$1 AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP)
                ORDER BY created_at DESC
                """,
                self.guild_id,
            )
        profiles = await asyncio.gather(*(
            self._blacklist_profile(int(row["user_id"])) for row in rows
        ))
        entries = []
        for row, profile in zip(rows, profiles):
            entries.append({
                **profile,
                "user_id": str(row["user_id"]),
                "reason": row["reason"],
                "added_by": str(row["added_by"]) if row["added_by"] else "",
                "blacklist_type": row["blacklist_type"],
                "expires_at": row["expires_at"],
                "created_at": row["created_at"],
            })
        return web.json_response(
            {"blacklist": entries},
            dumps=lambda value: json.dumps(value, ensure_ascii=False, default=str),
        )

    async def admin_add_blacklist(self, request):
        admin = await self._admin(request)
        data = await self._body(request)
        user_id = self._parse_discord_user_id(data.get("user_id"))
        reason = str(data.get("reason") or "").strip()
        blacklist_type = str(data.get("blacklist_type") or "PERMANENT").upper()
        if blacklist_type not in {"PERMANENT", "TEMPORARY"}:
            raise StoreError("블랙리스트 종류를 올바르게 선택해 주세요.")
        expires_at = None
        if blacklist_type == "TEMPORARY":
            duration = str(data.get("duration") or "7")
            if duration in {"7", "30"}:
                expires_at = datetime.now().astimezone() + timedelta(days=int(duration))
            else:
                try:
                    expires_at = datetime.fromisoformat(str(data.get("expires_at") or "")).astimezone()
                except (ValueError, TypeError) as exc:
                    raise StoreError("임시 블랙리스트 종료일을 입력해 주세요.") from exc
                if expires_at <= datetime.now().astimezone():
                    raise StoreError("종료일은 현재 시간보다 뒤여야 합니다.")
        if len(reason) < 2:
            raise StoreError("블랙리스트 등록 사유를 2자 이상 입력해 주세요.")
        if len(reason) > 500:
            raise StoreError("블랙리스트 등록 사유는 500자 이하로 입력해 주세요.")
        profile = await self._blacklist_profile(user_id)
        if profile["username"] == "알 수 없음":
            raise StoreError("해당 사용자 ID의 Discord 사용자를 찾을 수 없습니다.", 404)
        async with self.bot.db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO web_blacklist (guild_id, user_id, reason, added_by, blacklist_type, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (guild_id, user_id) DO UPDATE SET
                    reason=EXCLUDED.reason,
                    added_by=EXCLUDED.added_by,
                    blacklist_type=EXCLUDED.blacklist_type,
                    expires_at=EXCLUDED.expires_at,
                    created_at=CURRENT_TIMESTAMP
                """,
                self.guild_id, user_id, reason, int(admin["id"]), blacklist_type, expires_at,
            )
        return web.json_response({"ok": True, "user_id": str(user_id)})

    async def admin_remove_blacklist(self, request):
        await self._admin(request)
        user_id = self._parse_discord_user_id(request.match_info["user_id"])
        async with self.bot.db_pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM web_blacklist WHERE guild_id=$1 AND user_id=$2",
                self.guild_id, user_id,
            )
        if result == "DELETE 0":
            raise StoreError("블랙리스트에서 해당 사용자를 찾을 수 없습니다.", 404)
        return web.json_response({"ok": True})

    async def admin_dashboard(self, request):
        await self._admin(request)
        period = request.query.get("period", "week")
        if period == "year":
            trend_query = """
                SELECT TO_CHAR(month, 'YY.MM') AS label,
                       COALESCE(SUM(o.cash_amount + o.points_used), 0) AS value
                FROM generate_series(
                    DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '11 months',
                    DATE_TRUNC('month', CURRENT_DATE), INTERVAL '1 month'
                ) month
                LEFT JOIN orders o ON o.guild_id=$1 AND o.source='WEB' AND o.status='APPROVED'
                  AND o.processed_at >= month AND o.processed_at < month + INTERVAL '1 month'
                GROUP BY month ORDER BY month
            """
        elif period == "month":
            trend_query = """
                SELECT TO_CHAR(day, 'MM-DD') AS label,
                       COALESCE(SUM(o.cash_amount + o.points_used), 0) AS value
                FROM generate_series(CURRENT_DATE - INTERVAL '29 days', CURRENT_DATE, INTERVAL '1 day') day
                LEFT JOIN orders o ON o.guild_id=$1 AND o.source='WEB' AND o.status='APPROVED'
                  AND o.processed_at >= day AND o.processed_at < day + INTERVAL '1 day'
                GROUP BY day ORDER BY day
            """
        else:
            period = "week"
            trend_query = """
                SELECT TO_CHAR(day, 'MM-DD') AS label,
                       COALESCE(SUM(o.cash_amount + o.points_used), 0) AS value
                FROM generate_series(CURRENT_DATE - INTERVAL '6 days', CURRENT_DATE, INTERVAL '1 day') day
                LEFT JOIN orders o ON o.guild_id=$1 AND o.source='WEB' AND o.status='APPROVED'
                  AND o.processed_at >= day AND o.processed_at < day + INTERVAL '1 day'
                GROUP BY day ORDER BY day
            """
        async with self.bot.db_pool.acquire() as conn:
            summary = await conn.fetchrow(
                """
                SELECT COUNT(*) FILTER (WHERE status='APPROVED') AS approved,
                       COUNT(*) FILTER (WHERE status='PENDING') AS pending,
                       COUNT(*) FILTER (WHERE status='CANCELLED') AS cancelled,
                       COALESCE(SUM(cash_amount + points_used) FILTER (WHERE status='APPROVED'), 0) AS revenue
                FROM orders WHERE guild_id=$1 AND source='WEB'
                """, self.guild_id,
            )
            trend = await conn.fetch(trend_query, self.guild_id)
            top = await conn.fetch(
                """
                WITH ranked AS (
                  SELECT COALESCE(c.name, '미분류') category, i.product_name,
                         SUM(i.quantity) quantity,
                         ROW_NUMBER() OVER (PARTITION BY COALESCE(c.name, '미분류') ORDER BY SUM(i.quantity) DESC) rank
                  FROM web_order_items i JOIN orders o ON o.order_id=i.order_id
                  LEFT JOIN web_products p ON p.id=i.product_id LEFT JOIN web_categories c ON c.id=p.category_id
                  WHERE o.guild_id=$1 AND o.status='APPROVED'
                  GROUP BY COALESCE(c.name, '미분류'), i.product_name
                ) SELECT category, product_name, quantity FROM ranked WHERE rank <= 5 ORDER BY category, quantity DESC
                """, self.guild_id,
            )
        summary_data = dict(summary)
        summary_data["revenue"] = int(summary_data.get("revenue") or 0)
        trend_data = [{**dict(row), "value": int(row["value"] or 0)} for row in trend]
        return web.json_response({"summary": summary_data, "period": period, "trend": trend_data, "top": [dict(r) for r in top]})

    async def admin_categories(self, request):
        await self._admin(request)
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.*, parent.name AS parent_name
                FROM web_categories c
                LEFT JOIN web_categories parent ON parent.id=c.parent_id
                WHERE c.guild_id=$1
                ORDER BY COALESCE(parent.sort_order, c.sort_order), parent.name,
                         c.parent_id NULLS FIRST, c.sort_order, c.name
                """,
                self.guild_id,
            )
        return web.json_response({"categories": [dict(row) for row in rows]}, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=str))

    async def admin_save_category(self, request):
        await self._admin(request); data = await self._body(request)
        name = str(data.get("name", "")).strip()
        if not name:
            raise StoreError("카테고리명을 입력해 주세요.")
        try:
            sort_order = int(data.get("sort_order", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise StoreError("정렬 순서는 숫자로 입력해 주세요.") from exc
        try:
            parent_id = int(data["parent_id"]) if data.get("parent_id") else None
        except (TypeError, ValueError) as exc:
            raise StoreError("대분류를 올바르게 선택해 주세요.") from exc
        async with self.bot.db_pool.acquire() as conn:
            category_id = int(data["id"]) if data.get("id") else None
            if category_id and parent_id == category_id:
                raise StoreError("카테고리 자신을 대분류로 선택할 수 없습니다.")
            if parent_id:
                parent = await conn.fetchrow(
                    "SELECT id, parent_id FROM web_categories WHERE id=$1 AND guild_id=$2",
                    parent_id, self.guild_id,
                )
                if not parent:
                    raise StoreError("선택한 대분류를 찾을 수 없습니다.")
                if parent["parent_id"]:
                    raise StoreError("하위 카테고리 아래에 카테고리를 추가할 수 없습니다.")
                if category_id and await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM web_categories WHERE parent_id=$1 AND guild_id=$2)",
                    category_id, self.guild_id,
                ):
                    raise StoreError("하위 카테고리가 있는 대분류는 다른 대분류 아래로 이동할 수 없습니다.")
            if data.get("id"):
                duplicate = await conn.fetchval(
                    "SELECT id FROM web_categories WHERE guild_id=$1 AND name=$2 AND id<>$3",
                    self.guild_id, name, category_id,
                )
                if duplicate:
                    raise StoreError("같은 이름의 카테고리가 이미 있습니다.")
                result = await conn.fetchrow(
                    """
                    UPDATE web_categories SET name=$3, sort_order=$4, parent_id=$5
                    WHERE id=$1 AND guild_id=$2
                    RETURNING id, guild_id, name, parent_id, sort_order, created_at
                    """, category_id, self.guild_id, name, sort_order, parent_id,
                )
                if not result:
                    raise StoreError("수정할 카테고리를 찾을 수 없습니다.", 404)
            else:
                result = await conn.fetchrow(
                    "SELECT id, guild_id, name, parent_id, sort_order, created_at FROM web_categories WHERE guild_id=$1 AND name=$2",
                    self.guild_id, name,
                )
                if result:
                    result = await conn.fetchrow(
                        """
                        UPDATE web_categories SET sort_order=$3, parent_id=$4
                        WHERE id=$1 AND guild_id=$2
                        RETURNING id, guild_id, name, parent_id, sort_order, created_at
                        """, result["id"], self.guild_id, sort_order, parent_id,
                    )
                else:
                    result = await conn.fetchrow(
                        """
                        INSERT INTO web_categories (guild_id,name,parent_id,sort_order)
                        VALUES ($1,$2,$3,$4)
                        RETURNING id, guild_id, name, parent_id, sort_order, created_at
                        """, self.guild_id, name, parent_id, sort_order,
                    )
        return web.json_response(
            {"ok": True, "category": dict(result)},
            dumps=lambda value: json.dumps(value, ensure_ascii=False, default=str),
        )

    async def admin_delete_category(self, request):
        await self._admin(request)
        async with self.bot.db_pool.acquire() as conn:
            await conn.execute("DELETE FROM web_categories WHERE id=$1 AND guild_id=$2", int(request.match_info["category_id"]), self.guild_id)
        return web.json_response({"ok": True})

    async def admin_products(self, request):
        await self._admin(request)
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT p.*, c.name category_name, c.parent_id,
                       parent.name parent_category_name
                FROM web_products p
                LEFT JOIN web_categories c ON c.id=p.category_id
                LEFT JOIN web_categories parent ON parent.id=c.parent_id
                WHERE p.guild_id=$1 AND p.is_active=TRUE
                ORDER BY p.updated_at DESC
                """,
                self.guild_id,
            )
        return web.json_response({"products": [dict(row) for row in rows]}, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=str))

    async def admin_save_product(self, request):
        data = await self._body(request)
        if data.get("id"):
            await self._product_manager(request, int(data["id"]))
        else:
            await self._admin(request)
        name = str(data.get("name", "")).strip()
        price = int(data.get("price", -1))
        if not data.get("category_id"):
            raise StoreError("카테고리를 먼저 선택해 주세요.")
        if not name or price < 0:
            raise StoreError("상품명과 가격을 올바르게 입력해 주세요.")
        image_url = str(data.get("image_url", "")).strip()
        if not image_url:
            raise StoreError("상품 이미지를 URL 또는 파일로 등록해 주세요.")
        option_groups = normalize_option_groups(data.get("option_groups", []))
        first_group = option_groups[0] if option_groups else {"label": "없음", "options": []}
        option_label, options = first_group["label"], first_group["options"]
        try:
            free_shipping_quantity = int(data.get("free_shipping_quantity") or 0)
        except (TypeError, ValueError):
            raise StoreError("단독 구매 무료배송 수량을 확인해 주세요.")
        if not 0 <= free_shipping_quantity <= 99:
            raise StoreError("단독 구매 무료배송 수량은 0~99개로 설정해 주세요.")
        args = (
            self.guild_id, int(data["category_id"]), name,
            str(data.get("description", "")).strip(), price, 0,
            image_url, option_label, options, json.dumps(option_groups, ensure_ascii=False), free_shipping_quantity,
            bool(data.get("is_active", True)),
        )
        async with self.bot.db_pool.acquire() as conn:
            category_is_leaf = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM web_categories c
                    WHERE c.id=$1 AND c.guild_id=$2
                      AND NOT EXISTS(
                          SELECT 1 FROM web_categories child
                          WHERE child.parent_id=c.id AND child.guild_id=$2
                      )
                )
                """,
                int(data["category_id"]), self.guild_id,
            )
            if not category_is_leaf:
                raise StoreError("상품은 하위 카테고리에만 등록할 수 있습니다.")
            if data.get("id"):
                await conn.execute("UPDATE web_products SET category_id=$3,name=$4,description=$5,price=$6,stock=$7,image_url=$8,option_label=$9,options=$10,option_groups_json=$11,free_shipping_quantity=$12,is_active=$13,updated_at=CURRENT_TIMESTAMP WHERE id=$1 AND guild_id=$2", int(data["id"]), *args)
                await conn.execute(
                    "UPDATE web_product_sources SET price_locked=TRUE WHERE product_id=$1 AND guild_id=$2",
                    int(data["id"]), self.guild_id,
                )
            else:
                await conn.execute("INSERT INTO web_products (guild_id,category_id,name,description,price,stock,image_url,option_label,options,option_groups_json,free_shipping_quantity,is_active) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)", *args)
        return web.json_response({"ok": True})

    async def admin_sync_products(self, request):
        await self._admin(request)
        if self._product_sync_lock.locked():
            raise StoreError("상품 동기화가 이미 진행 중입니다. 잠시 후 다시 확인해 주세요.", 409)
        async with self._product_sync_lock:
            try:
                grouped, errors = await scrape_device_stores()
            except RuntimeError as exc:
                raise StoreError(str(exc), 502) from exc
            async with self.bot.db_pool.acquire() as conn:
                summary = await sync_scraped_products(conn, self.guild_id, grouped)
        summary["total"] = sum(len(products) for products in grouped.values())
        summary["unknown_options"] = sum(
            product.options == ["색상은 구매티켓에서 확인"]
            for products in grouped.values() for product in products
        )
        summary["sources"] = {site: len(products) for site, products in grouped.items()}
        summary["warnings"] = errors
        return web.json_response(summary, dumps=lambda value: json.dumps(value, ensure_ascii=False))

    async def admin_import_products(self, request):
        await self._admin(request)
        data = await self._body(request)
        source_url = str(data.get("source_url", "")).strip()
        try:
            category_id = int(data.get("category_id", 0))
        except (TypeError, ValueError):
            category_id = 0
        if not category_id:
            raise StoreError("상품을 넣을 카테고리를 선택해 주세요.")
        if not source_url:
            raise StoreError("상품 또는 목록 링크를 입력해 주세요.")
        try:
            manual_price = int(data.get("price", -1))
        except (TypeError, ValueError):
            manual_price = -1
        if manual_price < 0:
            raise StoreError("판매 가격을 올바르게 입력해 주세요.")
        option_groups = normalize_option_groups(data.get("option_groups", []))
        first_group = option_groups[0] if option_groups else {"label": "없음", "options": []}
        option_label, manual_options = first_group["label"], first_group["options"]
        try:
            free_shipping_quantity = int(data.get("free_shipping_quantity") or 0)
        except (TypeError, ValueError):
            raise StoreError("단독 구매 무료배송 수량을 확인해 주세요.")
        if not 0 <= free_shipping_quantity <= 99:
            raise StoreError("단독 구매 무료배송 수량은 0~99개로 설정해 주세요.")
        if self._product_sync_lock.locked():
            raise StoreError("다른 상품 가져오기가 진행 중입니다. 잠시 후 다시 시도해 주세요.", 409)

        async with self.bot.db_pool.acquire() as conn:
            category_exists = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM web_categories c
                    WHERE c.id=$1 AND c.guild_id=$2
                      AND NOT EXISTS(
                          SELECT 1 FROM web_categories child
                          WHERE child.parent_id=c.id AND child.guild_id=$2
                      )
                )
                """,
                category_id, self.guild_id,
            )
        if not category_exists:
            raise StoreError("상품을 넣을 하위 카테고리를 찾을 수 없습니다.", 404)

        async with self._product_sync_lock:
            try:
                products = await scrape_products_from_url(source_url)
            except RuntimeError as exc:
                raise StoreError(str(exc), 422) from exc
            except aiohttp.ClientResponseError as exc:
                raise StoreError(
                    f"쇼핑몰 요청이 거부되었습니다. (HTTP {exc.status}) 잠시 후 다시 시도해 주세요.",
                    502,
                ) from exc
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
                raise StoreError(
                    "쇼핑몰 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.", 502,
                ) from exc
            except aiohttp.ClientError as exc:
                raise StoreError("쇼핑몰 응답을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.", 502) from exc
            for product in products:
                product.option_label = option_label
                product.options = manual_options.copy()
            for product in products:
                product.price = manual_price
            async with self.bot.db_pool.acquire() as conn:
                summary = await import_scraped_products(
                    conn, self.guild_id, category_id, products, price_locked=True,
                )
                groups_json = json.dumps(option_groups, ensure_ascii=False)
                for product in products:
                    await conn.execute(
                        """
                        UPDATE web_products p SET option_groups_json=$4, free_shipping_quantity=$5, updated_at=CURRENT_TIMESTAMP
                        FROM web_product_sources s
                        WHERE s.product_id=p.id AND s.guild_id=$1
                          AND s.source_site=$2 AND s.source_key=$3
                        """,
                        self.guild_id, product.source_site, product.source_key, groups_json, free_shipping_quantity,
                    )
        return web.json_response(
            summary, dumps=lambda value: json.dumps(value, ensure_ascii=False),
        )

    async def admin_delete_product(self, request):
        product_id = int(request.match_info["product_id"])
        await self._product_manager(request, product_id)
        async with self.bot.db_pool.acquire() as conn:
            async with conn.transaction():
                deleted = await conn.fetchrow(
                    "UPDATE web_products SET is_active=FALSE, updated_at=CURRENT_TIMESTAMP WHERE id=$1 AND guild_id=$2 AND is_active=TRUE RETURNING name",
                    product_id, self.guild_id,
                )
                if deleted:
                    await conn.execute(
                        "DELETE FROM web_cart_items WHERE product_id=$1 AND guild_id=$2",
                        product_id, self.guild_id,
                    )
                    await conn.execute(
                        "UPDATE web_product_sources SET disabled_by_admin=TRUE WHERE product_id=$1 AND guild_id=$2",
                        product_id, self.guild_id,
                    )
        if not deleted:
            raise StoreError("삭제할 상품을 찾을 수 없습니다.", 404)
        return web.json_response({"ok": True, "name": deleted["name"]})

    async def admin_delete_products_batch(self, request):
        await self._admin(request)
        data = await self._body(request)
        raw_ids = data.get("product_ids", [])
        if not isinstance(raw_ids, list):
            raise StoreError("삭제할 상품을 선택해 주세요.")
        try:
            product_ids = list(dict.fromkeys(int(value) for value in raw_ids if int(value) > 0))
        except (TypeError, ValueError):
            raise StoreError("삭제할 상품 목록이 올바르지 않습니다.")
        if not product_ids:
            raise StoreError("삭제할 상품을 한 개 이상 선택해 주세요.")
        if len(product_ids) > 2_000:
            raise StoreError("한 번에 삭제할 수 있는 상품은 최대 2,000개입니다.")

        async with self.bot.db_pool.acquire() as conn:
            async with conn.transaction():
                deleted = await conn.fetch(
                    """
                    UPDATE web_products
                    SET is_active=FALSE, updated_at=CURRENT_TIMESTAMP
                    WHERE guild_id=$1 AND id=ANY($2::bigint[]) AND is_active=TRUE
                    RETURNING id,name
                    """,
                    self.guild_id, product_ids,
                )
                deleted_ids = [int(row["id"]) for row in deleted]
                if deleted_ids:
                    await conn.execute(
                        "DELETE FROM web_cart_items WHERE guild_id=$1 AND product_id=ANY($2::bigint[])",
                        self.guild_id, deleted_ids,
                    )
                    await conn.execute(
                        """
                        UPDATE web_product_sources SET disabled_by_admin=TRUE
                        WHERE guild_id=$1 AND product_id=ANY($2::bigint[])
                        """,
                        self.guild_id, deleted_ids,
                    )
        if not deleted:
            raise StoreError("삭제할 상품을 찾을 수 없습니다.", 404)
        return web.json_response({
            "ok": True,
            "deleted": len(deleted),
            "names": [row["name"] for row in deleted[:20]],
        })

    async def admin_channels(self, request):
        await self._admin(request)
        async with self.bot.db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT approval_channel_id,log_channel_id,buyer_info_channel_id,review_channel_id,inquiry_alert_channel_id,live_chat_channel_id,purchase_verified_role_id,live_chat_start,live_chat_end FROM guild_settings WHERE guild_id=$1", self.guild_id)
        channels = {
            key: str(value) if value is not None else ""
            for key, value in (dict(row).items() if row else [])
        }
        return web.json_response({"channels": channels})

    async def admin_save_channels(self, request):
        await self._admin(request); data = await self._body(request)
        keys = ("approval_channel_id", "log_channel_id", "buyer_info_channel_id", "review_channel_id", "inquiry_alert_channel_id", "live_chat_channel_id", "purchase_verified_role_id")
        raw_values = [str(data.get(key, "")).strip() for key in keys]
        if any(value and (not value.isdigit() or len(value) > 20) for value in raw_values):
            raise StoreError("Discord 채널·역할 ID는 숫자만 입력해 주세요.")
        values = [int(value) if value else None for value in raw_values]
        verified_role_id = values[-1]
        guild = self._guild()
        if verified_role_id and (guild is None or guild.get_role(verified_role_id) is None):
            raise StoreError("구매 인증 역할을 서버에서 찾을 수 없습니다. 역할 ID를 확인해 주세요.")
        chat_start = str(data.get("live_chat_start") or "10:00").strip()
        chat_end = str(data.get("live_chat_end") or "22:00").strip()
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", chat_start) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", chat_end):
            raise StoreError("상담시간을 HH:MM 형식으로 입력해 주세요.")
        async with self.bot.db_pool.acquire() as conn:
            await conn.execute("INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT DO NOTHING", self.guild_id)
            await conn.execute("UPDATE guild_settings SET approval_channel_id=$2,log_channel_id=$3,buyer_info_channel_id=$4,review_channel_id=$5,inquiry_alert_channel_id=$6,live_chat_channel_id=$7,purchase_verified_role_id=$8,live_chat_start=$9,live_chat_end=$10 WHERE guild_id=$1", self.guild_id, *values, chat_start, chat_end)
        return web.json_response({
            "ok": True,
            "channels": {**{key: str(value) if value is not None else "" for key, value in zip(keys, values)}, "live_chat_start": chat_start, "live_chat_end": chat_end},
        })

    async def admin_orders(self, request):
        await self._admin(request)
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT order_id,buyer_id,product,quantity,amount,status,created_at,processed_at,
                          shipping_name,shipping_contact,shipping_address,shipping_cvs,shipping_method,shipping_memo,
                          shipping_fee,points_used,cash_amount,waybill_number,carrier_id,
                          fulfillment_status,fulfillment_updated_at,cancel_reason
                   FROM orders WHERE guild_id=$1 AND source='WEB'
                   ORDER BY created_at DESC LIMIT 200""", self.guild_id,
            )
        guild = self._guild()
        result = []
        for row in rows:
            item = dict(row)
            member = guild.get_member(int(row["buyer_id"])) if guild else None
            item["buyer_name"] = member.display_name if member else str(row["buyer_id"])
            result.append(item)
        return web.json_response({"orders": result}, dumps=lambda value: json.dumps(value, ensure_ascii=False, default=str))

    async def admin_cancel_order(self, request):
        order_id = request.match_info["order_id"]
        await self._order_manager(request, order_id)
        data = await self._body(request)
        reason = str(data.get("reason", "")).strip()
        if len(reason) < 2:
            raise StoreError("주문취소 사유를 입력해 주세요.")
        async with self.bot.db_pool.acquire() as conn:
            async with conn.transaction():
                order = await conn.fetchrow(
                    "SELECT * FROM orders WHERE order_id=$1 AND guild_id=$2 AND source='WEB' FOR UPDATE",
                    order_id, self.guild_id,
                )
                if not order or order["status"] in {"CANCELLED", "REJECTED"}:
                    raise StoreError("취소할 수 있는 주문을 찾을 수 없습니다.", 404)
                points = int(order.get("points_used") or 0)
                if points and not order.get("points_refunded"):
                    await conn.execute(
                        "INSERT INTO point_balances (guild_id,user_id,balance) VALUES ($1,$2,$3) ON CONFLICT (guild_id,user_id) DO UPDATE SET balance=point_balances.balance+$3",
                        self.guild_id, int(order["buyer_id"]), points,
                    )
                await conn.execute(
                    "UPDATE orders SET status='CANCELLED',cancel_reason=$2,points_refunded=CASE WHEN points_used>0 THEN TRUE ELSE points_refunded END,processed_at=CURRENT_TIMESTAMP WHERE order_id=$1",
                    order_id, reason,
                )
                await conn.execute("UPDATE web_coupons SET used_order_id=NULL WHERE guild_id=$1 AND used_order_id=$2", self.guild_id, order_id)
                if order["status"] == "APPROVED":
                    await conn.execute(
                        "UPDATE user_info SET total_spent=GREATEST(COALESCE(total_spent,0)-$2,0) WHERE user_id=$1",
                        int(order["buyer_id"]), parse_amount(order["amount"]),
                    )
                refund_account = await conn.fetchval("SELECT refund_account FROM user_info WHERE user_id=$1", int(order["buyer_id"]))
        payload = {
            "flags": 1 << 15,
            "components": [{"type": 17, "accent_color": 0xE74C3C, "components": [
                {"type": 10, "content": f"## 주문이 취소되었습니다\n\n`주문번호` {order_id}\n`취소 사유` {reason}\n\n결제하신 금액은 확인 후 환불됩니다."},
                *([] if refund_account else [{"type": 1, "components": [{"type": 2, "style": 1, "label": "환불계좌 등록", "custom_id": f"web_refund_account:{order_id}"}]}]),
            ]}],
        }
        try:
            user = self.bot.get_user(int(order["buyer_id"])) or await self.bot.fetch_user(int(order["buyer_id"]))
            dm = await user.create_dm()
            await self.bot.http.request(discord.http.Route("POST", f"/channels/{dm.id}/messages"), json=payload)
        except Exception:
            LOGGER.exception("주문취소 DM 실패: %s", order_id)
        if refund_account:
            try:
                owner_id = int(os.getenv("BOT_OWNER_ID", "0") or 0)
                owner = self.bot.get_user(owner_id) if owner_id else None
                if owner is None:
                    owner = (await self.bot.application_info()).owner
                owner_dm = await owner.create_dm()
                owner_payload = {
                    "flags": 1 << 15,
                    "components": [{"type": 17, "accent_color": 0x32CD32, "components": [{
                        "type": 10,
                        "content": f"## 💳 주문취소 환불정보\n\n`유저` {order['buyer_id']}\n`주문` {order['product']}\n`주문 ID` {order_id}\n`환불계좌` {refund_account}",
                    }]}],
                }
                await self.bot.http.request(discord.http.Route("POST", f"/channels/{owner_dm.id}/messages"), json=owner_payload)
            except Exception:
                LOGGER.exception("기존 환불계좌 소유자 DM 실패: %s", order_id)
        if order["status"] == "APPROVED":
            guild = self._guild()
            if guild:
                try:
                    await sync_buyer_roles(self.bot, guild, int(order["buyer_id"]))
                except Exception:
                    LOGGER.exception("주문취소 후 구매자 역할 동기화 실패: %s", order_id)
            refresh = getattr(self.bot, "refresh_purchase_leaderboard", None)
            if refresh:
                try:
                    await refresh(self.guild_id)
                except Exception:
                    LOGGER.exception("주문취소 후 구매랭킹 동기화 실패: %s", order_id)
        return web.json_response({"ok": True})

    async def admin_set_waybill(self, request):
        order_id = request.match_info["order_id"]
        await self._order_manager(request, order_id)
        data = await self._body(request)
        try:
            waybill = normalize_waybill(str(data.get("waybill", "")))
        except DeliveryTrackingError as exc:
            raise StoreError(str(exc)) from exc
        async with self.bot.db_pool.acquire() as conn:
            order = await conn.fetchrow("SELECT buyer_id,shipping_method,product FROM orders WHERE order_id=$1 AND guild_id=$2 AND source='WEB' AND status='APPROVED'", order_id, self.guild_id)
            if not order:
                raise StoreError("승인된 웹 주문을 찾을 수 없습니다.", 404)
            method = str(order["shipping_method"] or "")
            carrier = "kr.cvsnet" if "GS25" in method else "kr.cupost" if "CU" in method else "kr.cjlogistics"
            await conn.execute("UPDATE orders SET waybill_number=$2,carrier_id=$3,fulfillment_status='SHIPPING_PREPARING',fulfillment_updated_at=CURRENT_TIMESTAMP,last_tracking_event=NULL,last_tracking_notified_at=NULL WHERE order_id=$1", order_id, waybill, carrier)
        try:
            user = self.bot.get_user(int(order["buyer_id"])) or await self.bot.fetch_user(int(order["buyer_id"]))
            dm = await user.create_dm()
            payload = {
                "flags": 1 << 15,
                "components": [{"type": 17, "accent_color": 0x32CD32, "components": [{
                    "type": 10,
                    "content": f"## 🚚 운송장이 발급되었습니다\n\n`◼️` **주문번호** : `{order_id}`\n`◼️` **운송장** : `{waybill}`\n`◼️` **상품** : `{order['product']}`\n`◼️` **진행상황** : **배송준비중 (60%)**\n\n> 웹사이트 **내 정보 → 주문내역 → 상세조회**에서 배송 현황을 확인할 수 있습니다.",
                }]}],
            }
            await self.bot.http.request(discord.http.Route("POST", f"/channels/{dm.id}/messages"), json=payload)
        except Exception:
            LOGGER.exception("웹 운송장 DM 실패: %s", order_id)
        return web.json_response({"ok": True, "waybill": waybill})

    async def admin_delete_order(self, request):
        order_id = request.match_info["order_id"]
        await self._order_manager(request, order_id)
        async with self.bot.db_pool.acquire() as conn:
            async with conn.transaction():
                status = await conn.fetchval(
                    "SELECT status FROM orders WHERE order_id=$1 AND guild_id=$2 AND source='WEB' FOR UPDATE",
                    order_id, self.guild_id,
                )
                if status not in {"CANCELLED", "REJECTED"}:
                    raise StoreError("취소되거나 거절된 주문 기록만 삭제할 수 있습니다.", 409)
                await conn.execute("DELETE FROM web_order_items WHERE order_id=$1", order_id)
                await conn.execute(
                    "DELETE FROM orders WHERE order_id=$1 AND guild_id=$2 AND source='WEB'",
                    order_id, self.guild_id,
                )
        return web.json_response({"ok": True})

    async def _payment_approved(self, order: dict, notice: DepositNotice):
        await finalize_approved_order(self.bot, order, processor=f"Pushbullet · {notice.application_name or notice.package_name}")

    async def _payment_expired(self, order: dict):
        async with self.bot.db_pool.acquire() as conn:
            await conn.execute("UPDATE web_coupons SET used_order_id=NULL WHERE guild_id=$1 AND used_order_id=$2", int(order["guild_id"]), order["order_id"])
        await notify_expired_order(self.bot, order)


async def start_web_store(bot) -> StoreServer:
    server = StoreServer(bot)
    await server.start()
    return server

