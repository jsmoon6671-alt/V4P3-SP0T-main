"""Discord 로그인 기반 V4P3 SP0T 웹 스토어와 봇 연동 서버."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

from loyalty_points import PointsError, get_balance, maximum_points, request_payment, resolve_payment
from order_fulfillment import finalize_approved_order, notify_expired_order
from payment_automation import (
    DepositNotice,
    PushbulletPaymentService,
    arm_web_payment,
    initialize_payment_automation_schema,
)
from store_lookup import StoreLookupError, search_stores


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "web" / "static"
COOKIE = "v4p3_session"
ADMIN_COOKIE = "v4p3_admin"
SHIPPING_FEE = 3_000


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
            PRIMARY KEY (order_id, product_id)
        );
        ALTER TABLE web_products ADD COLUMN IF NOT EXISTS option_label TEXT NOT NULL DEFAULT '색상';
        ALTER TABLE web_products ADD COLUMN IF NOT EXISTS options TEXT[] NOT NULL DEFAULT '{}';
        ALTER TABLE web_cart_items ADD COLUMN IF NOT EXISTS selected_option TEXT NOT NULL DEFAULT '';
        ALTER TABLE web_order_items ADD COLUMN IF NOT EXISTS product_option TEXT NOT NULL DEFAULT '';
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_name TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_contact TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_address TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_cvs TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_method TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_fee BIGINT NOT NULL DEFAULT 0;
        """
    )
    await initialize_payment_automation_schema(conn)


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
        self.admin_password = os.getenv("WEB_ADMIN_PASSWORD", "").strip() or "tb9988230.."
        self.runner: web.AppRunner | None = None
        self.payment_service = PushbulletPaymentService(
            bot,
            self._payment_approved,
            self._payment_expired,
        )
        self.app = web.Application(client_max_size=8 * 1024 ** 2, middlewares=[self._errors, self._session])
        self._routes()

    @web.middleware
    async def _errors(self, request, handler):
        try:
            return await handler(request)
        except StoreError as exc:
            return web.json_response({"error": str(exc)}, status=exc.status)
        except PointsError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    @web.middleware
    async def _session(self, request, handler):
        request["session"] = self._decode(request.cookies.get(COOKIE, ""))
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.path.startswith("/api/"):
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
            web.get("/api/me", self.me),
            web.get("/api/catalog", self.catalog),
            web.get("/api/stores", self.stores),
            web.get("/api/customer", self.customer),
            web.put("/api/customer", self.save_customer),
            web.get("/api/cart", self.cart),
            web.post("/api/cart", self.save_cart),
            web.delete(r"/api/cart/{product_id:\d+}", self.delete_cart),
            web.post("/api/checkout", self.checkout),
            web.get("/api/orders", self.orders),
            web.get(r"/api/orders/{order_id}", self.order),
            web.post("/api/admin/unlock", self.admin_unlock),
            web.get("/api/admin/dashboard", self.admin_dashboard),
            web.get("/api/admin/categories", self.admin_categories),
            web.post("/api/admin/categories", self.admin_save_category),
            web.delete(r"/api/admin/categories/{category_id:\d+}", self.admin_delete_category),
            web.get("/api/admin/products", self.admin_products),
            web.post("/api/admin/products", self.admin_save_product),
            web.delete(r"/api/admin/products/{product_id:\d+}", self.admin_delete_product),
            web.get("/api/admin/channels", self.admin_channels),
            web.put("/api/admin/channels", self.admin_save_channels),
        ]
        self.app.add_routes(routes)
        self.app.router.add_static("/static", STATIC, show_index=False)
        self.app.router.add_get(r"/{tail:.*}", self.index)

    async def start(self):
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

    async def _user(self, request) -> dict:
        session = request["session"]
        if not session:
            raise StoreError("Discord 로그인이 필요합니다.", 401)
        return session

    async def _admin_member(self, request) -> dict:
        session = await self._user(request)
        guild = self._guild()
        if guild is None:
            raise StoreError("WEB_GUILD_ID 서버를 찾을 수 없습니다.", 503)
        member = guild.get_member(int(session["id"]))
        if member is None:
            try:
                member = await guild.fetch_member(int(session["id"]))
            except Exception:
                raise StoreError("해당 Discord 서버의 멤버만 이용할 수 있습니다.", 403)
        async with self.bot.db_pool.acquire() as conn:
            role_rows = await conn.fetch("SELECT role_id FROM guild_admin_roles WHERE guild_id = $1", guild.id)
        role_ids = {int(row["role_id"]) for row in role_rows}
        if not member.guild_permissions.administrator and not any(role.id in role_ids for role in member.roles):
            raise StoreError("관리자 전용 메뉴입니다.", 403)
        return session

    async def _admin(self, request) -> dict:
        session = await self._admin_member(request)
        unlocked = self._decode(request.cookies.get(ADMIN_COOKIE, ""))
        if not unlocked or str(unlocked.get("admin_for")) != str(session["id"]):
            raise StoreError("관리자 비밀번호를 입력해 주세요.", 403)
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
            "scope": "identify",
            "state": state,
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
            raise StoreError("판매 Discord 서버에 가입한 사용자만 이용할 수 있습니다.", 403)
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
        response.del_cookie(ADMIN_COOKIE)
        return response

    async def me(self, request):
        user = await self._user(request)
        is_admin = True
        try:
            await self._admin_member(request)
        except StoreError:
            is_admin = False
        return web.json_response({"user": {"id": user["id"], "username": user["username"], "avatar": user.get("avatar")}, "csrf": user["csrf"], "is_admin": is_admin})

    async def admin_unlock(self, request):
        user = await self._admin_member(request)
        data = await self._body(request)
        password = str(data.get("password", ""))
        if not hmac.compare_digest(password, self.admin_password):
            raise StoreError("관리자 비밀번호가 올바르지 않습니다.", 403)
        payload = {"admin_for": str(user["id"]), "exp": int(time.time()) + 60 * 60 * 8}
        response = web.json_response({"ok": True})
        response.set_cookie(
            ADMIN_COOKIE, self._encode(payload), httponly=True, secure=True,
            samesite="Strict", max_age=60 * 60 * 8,
        )
        return response

    async def catalog(self, request):
        guild = self._guild()
        guild_id = guild.id if guild else self.guild_id
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT p.*, c.name AS category_name FROM web_products p
                LEFT JOIN web_categories c ON c.id = p.category_id
                WHERE p.guild_id = $1 AND p.is_active = TRUE
                ORDER BY c.sort_order, c.name, p.name
                """, guild_id,
            )
        return web.json_response(
            {"products": [dict(row) for row in rows]},
            dumps=lambda value: json.dumps(value, ensure_ascii=False, default=str),
        )

    async def stores(self, request):
        await self._user(request)
        brand = str(request.query.get("brand", "")).strip().upper()
        query = str(request.query.get("q", "")).strip()
        try:
            results = await search_stores(brand, query)
        except StoreLookupError as exc:
            raise StoreError(str(exc)) from exc
        return web.json_response({"stores": results[:25]}, dumps=lambda value: json.dumps(value, ensure_ascii=False))

    async def customer(self, request):
        user = await self._user(request)
        async with self.bot.db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT name, contact, address, cvs FROM user_info WHERE user_id = $1", int(user["id"]))
            balance = await get_balance(conn, self.guild_id, int(user["id"]))
        return web.json_response({"customer": dict(row) if row else {}, "points": balance})

    async def save_customer(self, request):
        user, data = await self._user(request), await self._body(request)
        values = [str(data.get(key, "")).strip() for key in ("name", "contact", "address", "cvs")]
        if not all(values[:3]):
            raise StoreError("이름, 연락처, 주소를 모두 입력해 주세요.")
        async with self.bot.db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO user_info (user_id, name, contact, address, cvs)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id) DO UPDATE SET
                    name = EXCLUDED.name, contact = EXCLUDED.contact,
                    address = EXCLUDED.address, cvs = EXCLUDED.cvs
                """, int(user["id"]), *values,
            )
        return web.json_response({"ok": True})

    async def cart(self, request):
        user = await self._user(request)
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.product_id, c.quantity, c.selected_option, p.name, p.price,
                       p.stock, p.image_url, p.option_label, p.options,
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
        selected_option = str(data.get("selected_option", "")).strip()
        if not 1 <= quantity <= 99:
            raise StoreError("수량은 1~99개로 입력해 주세요.")
        async with self.bot.db_pool.acquire() as conn:
            product = await conn.fetchrow("SELECT stock, is_active, options FROM web_products WHERE id = $1 AND guild_id = $2", product_id, self.guild_id)
            if not product or not product["is_active"]:
                raise StoreError("판매 중인 상품이 아닙니다.", 404)
            if quantity > product["stock"]:
                raise StoreError("현재 재고보다 많은 수량입니다.")
            options = list(product["options"] or [])
            if options and selected_option not in options:
                raise StoreError("상품의 색상 또는 맛을 선택해 주세요.")
            await conn.execute(
                """
                INSERT INTO web_cart_items (guild_id, user_id, product_id, quantity, selected_option)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (guild_id, user_id, product_id) DO UPDATE SET
                    quantity = EXCLUDED.quantity, selected_option = EXCLUDED.selected_option
                """, self.guild_id, int(user["id"]), product_id, quantity, selected_option,
            )
        return web.json_response({"ok": True})

    async def delete_cart(self, request):
        user = await self._user(request)
        async with self.bot.db_pool.acquire() as conn:
            await conn.execute("DELETE FROM web_cart_items WHERE guild_id = $1 AND user_id = $2 AND product_id = $3", self.guild_id, int(user["id"]), int(request.match_info["product_id"]))
        return web.json_response({"ok": True})

    async def checkout(self, request):
        user, data = await self._user(request), await self._body(request)
        if not data.get("adult_confirmed"):
            raise StoreError("성인 확인에 동의해야 주문할 수 있습니다.")
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
        if not shipping_name or not shipping_contact or not shipping_address:
            raise StoreError("받는 분의 이름, 연락처, 배송 주소를 모두 입력해 주세요.")
        if shipping_method in {"GS25", "CU"} and not shipping_cvs:
            raise StoreError("조회 결과에서 받을 편의점을 선택해 주세요.")
        if shipping_method == "GENERAL":
            shipping_cvs = ""
        points = int(data.get("points", 0) or 0)
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
                    SELECT c.product_id, c.quantity, c.selected_option,
                           p.name, p.price, p.stock, p.is_active, p.options
                    FROM web_cart_items c JOIN web_products p ON p.id = c.product_id
                    WHERE c.guild_id = $1 AND c.user_id = $2 AND c.product_id = ANY($3::bigint[])
                    ORDER BY c.product_id FOR UPDATE OF p
                    """, self.guild_id, user_id, selected,
                )
                if len(rows) != len(set(selected)):
                    raise StoreError("장바구니 상품 일부를 찾을 수 없습니다.")
                if any(not row["is_active"] or row["quantity"] > row["stock"] for row in rows):
                    raise StoreError("판매가 종료되었거나 재고가 부족한 상품이 있습니다.")
                for row in rows:
                    options = list(row["options"] or [])
                    if options and row["selected_option"] not in options:
                        raise StoreError(f"{row['name']} 상품의 색상 또는 맛을 다시 선택해 주세요.")
                subtotal = sum(int(row["price"]) * int(row["quantity"]) for row in rows)
                total = subtotal + SHIPPING_FEE
                if not self.payment_service.enabled and total - points > 0:
                    raise StoreError("Pushbullet 자동결제가 아직 설정되지 않았습니다. 관리자에게 문의해 주세요.", 503)
                product_text = ", ".join(
                    f"{row['name']} ({row['selected_option']})" if row["selected_option"] else row["name"]
                    for row in rows
                )
                quantity_text = f"총 {sum(int(row['quantity']) for row in rows)}개"
                await conn.execute(
                    """
                    INSERT INTO orders (
                        order_id, guild_id, original_channel_id, buyer_id, product,
                        quantity, amount, status, points_allowed, source,
                        shipping_name, shipping_contact, shipping_address, shipping_cvs,
                        shipping_method, shipping_fee
                    ) VALUES ($1, $2, NULL, $3, $4, $5, $6, 'PENDING', TRUE, 'WEB',
                              $7, $8, $9, $10, $11, $12)
                    """, order_id, self.guild_id, user_id, product_text, quantity_text, f"{total:,}원",
                    shipping_name, shipping_contact, shipping_address, shipping_cvs,
                    shipping_labels[shipping_method], SHIPPING_FEE,
                )
                for row in rows:
                    await conn.execute(
                        """
                        INSERT INTO web_order_items
                            (order_id, product_id, product_name, unit_price, quantity, product_option)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        """, order_id, row["product_id"], row["name"], row["price"],
                        row["quantity"], row["selected_option"],
                    )
                    await conn.execute("UPDATE web_products SET stock = stock - $2, updated_at = CURRENT_TIMESTAMP WHERE id = $1", row["product_id"], row["quantity"])
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
            "shipping_fee": SHIPPING_FEE,
            "deadline_seconds": 300,
            "bank": dict(settings) if settings else {},
        })

    async def orders(self, request):
        user = await self._user(request)
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT order_id, product, quantity, amount, status, points_used, cash_amount,
                       created_at, processed_at, payment_deadline, cancel_reason
                FROM orders WHERE guild_id = $1 AND buyer_id = $2 AND source = 'WEB'
                ORDER BY created_at DESC LIMIT 100
                """, self.guild_id, int(user["id"]),
            )
        return web.json_response({"orders": [dict(row) for row in rows]}, dumps=lambda value: json.dumps(value, ensure_ascii=False, default=str))

    async def order(self, request):
        user = await self._user(request)
        async with self.bot.db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM orders WHERE order_id = $1 AND guild_id = $2 AND buyer_id = $3 AND source = 'WEB'", request.match_info["order_id"], self.guild_id, int(user["id"]))
        if not row:
            raise StoreError("주문을 찾을 수 없습니다.", 404)
        return web.json_response(dict(row), dumps=lambda value: json.dumps(value, ensure_ascii=False, default=str))

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
            rows = await conn.fetch("SELECT * FROM web_categories WHERE guild_id=$1 ORDER BY sort_order, name", self.guild_id)
        return web.json_response({"categories": [dict(row) for row in rows]}, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=str))

    async def admin_save_category(self, request):
        await self._admin(request); data = await self._body(request)
        name = str(data.get("name", "")).strip()
        if not name: raise StoreError("카테고리명을 입력해 주세요.")
        async with self.bot.db_pool.acquire() as conn:
            if data.get("id"):
                await conn.execute("UPDATE web_categories SET name=$3, sort_order=$4 WHERE id=$1 AND guild_id=$2", int(data["id"]), self.guild_id, name, int(data.get("sort_order", 0)))
            else:
                await conn.execute("INSERT INTO web_categories (guild_id,name,sort_order) VALUES ($1,$2,$3)", self.guild_id, name, int(data.get("sort_order", 0)))
        return web.json_response({"ok": True})

    async def admin_delete_category(self, request):
        await self._admin(request)
        async with self.bot.db_pool.acquire() as conn:
            await conn.execute("DELETE FROM web_categories WHERE id=$1 AND guild_id=$2", int(request.match_info["category_id"]), self.guild_id)
        return web.json_response({"ok": True})

    async def admin_products(self, request):
        await self._admin(request)
        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT p.*, c.name category_name FROM web_products p LEFT JOIN web_categories c ON c.id=p.category_id WHERE p.guild_id=$1 ORDER BY p.updated_at DESC", self.guild_id)
        return web.json_response({"products": [dict(row) for row in rows]}, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=str))

    async def admin_save_product(self, request):
        await self._admin(request); data = await self._body(request)
        name = str(data.get("name", "")).strip()
        price, stock = int(data.get("price", -1)), int(data.get("stock", -1))
        if not data.get("category_id"):
            raise StoreError("카테고리를 먼저 선택해 주세요.")
        if not name or price < 0 or stock < 0:
            raise StoreError("상품명, 가격, 재고를 올바르게 입력해 주세요.")
        image_url = str(data.get("image_url", "")).strip()
        if not image_url:
            raise StoreError("상품 이미지를 URL 또는 파일로 등록해 주세요.")
        option_label = str(data.get("option_label", "색상")).strip()
        if option_label not in {"색상", "맛"}:
            raise StoreError("상품 옵션은 색상 또는 맛으로 선택해 주세요.")
        raw_options = data.get("options", [])
        if isinstance(raw_options, str):
            raw_options = raw_options.replace("\r", "\n").replace(",", "\n").split("\n")
        options = list(dict.fromkeys(str(value).strip() for value in raw_options if str(value).strip()))
        if not options:
            raise StoreError("색상 또는 맛을 한 개 이상 입력해 주세요.")
        if len(options) > 50:
            raise StoreError("색상 또는 맛은 최대 50개까지 등록할 수 있습니다.")
        args = (
            self.guild_id, int(data["category_id"]), name,
            str(data.get("description", "")).strip(), price, stock,
            image_url, option_label, options,
            bool(data.get("is_active", True)),
        )
        async with self.bot.db_pool.acquire() as conn:
            if data.get("id"):
                await conn.execute("UPDATE web_products SET category_id=$3,name=$4,description=$5,price=$6,stock=$7,image_url=$8,option_label=$9,options=$10,is_active=$11,updated_at=CURRENT_TIMESTAMP WHERE id=$1 AND guild_id=$2", int(data["id"]), *args)
            else:
                await conn.execute("INSERT INTO web_products (guild_id,category_id,name,description,price,stock,image_url,option_label,options,is_active) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)", *args)
        return web.json_response({"ok": True})

    async def admin_delete_product(self, request):
        await self._admin(request)
        async with self.bot.db_pool.acquire() as conn:
            await conn.execute("UPDATE web_products SET is_active=FALSE, updated_at=CURRENT_TIMESTAMP WHERE id=$1 AND guild_id=$2", int(request.match_info["product_id"]), self.guild_id)
        return web.json_response({"ok": True})

    async def admin_channels(self, request):
        await self._admin(request)
        async with self.bot.db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT approval_channel_id,log_channel_id,buyer_info_channel_id,review_channel_id FROM guild_settings WHERE guild_id=$1", self.guild_id)
        channels = {
            key: str(value) if value is not None else ""
            for key, value in (dict(row).items() if row else [])
        }
        return web.json_response({"channels": channels})

    async def admin_save_channels(self, request):
        await self._admin(request); data = await self._body(request)
        keys = ("approval_channel_id", "log_channel_id", "buyer_info_channel_id", "review_channel_id")
        values = [int(data[key]) if str(data.get(key, "")).strip() else None for key in keys]
        async with self.bot.db_pool.acquire() as conn:
            await conn.execute("INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT DO NOTHING", self.guild_id)
            await conn.execute("UPDATE guild_settings SET approval_channel_id=$2,log_channel_id=$3,buyer_info_channel_id=$4,review_channel_id=$5 WHERE guild_id=$1", self.guild_id, *values)
        return web.json_response({
            "ok": True,
            "channels": {key: str(value) if value is not None else "" for key, value in zip(keys, values)},
        })

    async def _payment_approved(self, order: dict, notice: DepositNotice):
        await finalize_approved_order(self.bot, order, processor=f"Pushbullet · {notice.application_name or notice.package_name}")

    async def _payment_expired(self, order: dict):
        await notify_expired_order(self.bot, order)


async def start_web_store(bot) -> StoreServer:
    server = StoreServer(bot)
    await server.start()
    return server

