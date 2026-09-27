"""서버별 후기 포인트와 주문 포인트 결제를 PostgreSQL 트랜잭션으로 관리합니다."""

import random
import re

MIN_POINTS = 500
MAX_POINTS = 2000


def maximum_points(balance, amount, allowed=True):
    maximum = min(balance, amount, MAX_POINTS) if allowed else 0
    return maximum if maximum >= MIN_POINTS else 0


REVIEW_GUIDE = (
    "- /후기작성 명령어를 사용하여 후기작성이 가능 합니다!\n\n"
    "> 후기 작성시 랜덤으로 100P \\~ 500P 의 포인트가 적립됩니다!"
)


class PointsError(Exception):
    pass


def parse_amount(value):
    text = re.sub(r"\s+", "", str(value)).removeprefix("₩")
    if not re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:원)?", text):
        raise PointsError("주문금액은 숫자로 입력해 주세요. 예: 99,800원")
    amount = int(text.removesuffix("원").replace(",", ""))
    if not 0 < amount <= 9_223_372_036_854_775_807:
        raise PointsError("주문금액은 1원 이상의 유효한 금액이어야 합니다.")
    return amount


def parse_points(value):
    if not re.fullmatch(r"[0-9]{1,19}", value.strip()):
        raise PointsError("사용할 포인트는 0 이상의 정수로 입력해 주세요. 사용하지 않으려면 0을 입력하세요.")
    return int(value.strip())


async def initialize_points_schema(conn):
    # 필요한 마이그레이션은 실패를 숨기지 않고 시작 단계에서 확인합니다.
    async with conn.transaction():
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS point_balances (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                balance BIGINT NOT NULL DEFAULT 0 CHECK (balance >= 0),
                PRIMARY KEY (guild_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS review_point_rewards (
                guild_id BIGINT NOT NULL,
                interaction_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                points INTEGER NOT NULL CHECK (points BETWEEN 100 AND 500),
                PRIMARY KEY (guild_id, interaction_id)
            );
            CREATE TABLE IF NOT EXISTS point_admin_adjustments (
                guild_id BIGINT NOT NULL,
                interaction_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                admin_id BIGINT NOT NULL,
                delta BIGINT NOT NULL CHECK (delta <> 0),
                balance_after BIGINT NOT NULL CHECK (balance_after >= 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, interaction_id)
            );
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS points_used BIGINT NOT NULL DEFAULT 0 CHECK (points_used >= 0);
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS points_refunded BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_requested BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS cash_amount BIGINT CHECK (cash_amount >= 0);
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS points_allowed BOOLEAN NOT NULL DEFAULT TRUE;
            UPDATE orders SET payment_requested = TRUE
                WHERE status = 'PENDING' AND NOT payment_requested
                AND depositor_name IS NOT NULL AND BTRIM(depositor_name) <> '';
        ''')


async def get_balance(conn, guild_id, user_id):
    return await conn.fetchval(
        'SELECT balance FROM point_balances WHERE guild_id = $1 AND user_id = $2', guild_id, user_id,
    ) or 0


async def _lock_balance(conn, guild_id, user_id):
    await conn.execute('''
        INSERT INTO point_balances (guild_id, user_id) VALUES ($1, $2)
        ON CONFLICT (guild_id, user_id) DO NOTHING
    ''', guild_id, user_id)
    return await conn.fetchval('''
        SELECT balance FROM point_balances WHERE guild_id = $1 AND user_id = $2 FOR UPDATE
    ''', guild_id, user_id)


async def adjust_points(conn, guild_id, user_id, admin_id, interaction_id, delta):
    if not isinstance(delta, int) or delta == 0 or abs(delta) > 9_223_372_036_854_775_807:
        raise PointsError("추가하거나 제거할 포인트는 1 이상의 정수로 입력해 주세요.")
    async with conn.transaction():
        balance = await _lock_balance(conn, guild_id, user_id)
        previous = await conn.fetchrow('''
            SELECT user_id, balance_after FROM point_admin_adjustments
            WHERE guild_id = $1 AND interaction_id = $2
        ''', guild_id, interaction_id)
        if previous is not None:
            if previous['user_id'] != user_id:
                raise PointsError("이미 처리된 포인트 변경 요청입니다.")
            return previous['balance_after']
        result = balance + delta
        if result < 0:
            raise PointsError(f"보유 포인트가 부족합니다. 현재 잔액: {balance:,}P")
        if result > 9_223_372_036_854_775_807:
            raise PointsError("저장 가능한 포인트 잔액을 초과합니다.")
        await conn.execute('''
            UPDATE point_balances SET balance = $3 WHERE guild_id = $1 AND user_id = $2
        ''', guild_id, user_id, result)
        await conn.execute('''
            INSERT INTO point_admin_adjustments (guild_id, interaction_id, user_id, admin_id, delta, balance_after)
            VALUES ($1, $2, $3, $4, $5, $6)
        ''', guild_id, interaction_id, user_id, admin_id, delta, result)
        return result


async def award_review_points(conn, guild_id, user_id, interaction_id):
    async with conn.transaction():
        reward = await conn.fetchval('''
            INSERT INTO review_point_rewards (guild_id, interaction_id, user_id, points)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id, interaction_id) DO NOTHING RETURNING points
        ''', guild_id, interaction_id, user_id, random.randint(100, 500))
        if reward is not None:
            await conn.execute('''
                INSERT INTO point_balances (guild_id, user_id, balance) VALUES ($1, $2, $3)
                ON CONFLICT (guild_id, user_id) DO UPDATE
                SET balance = point_balances.balance + EXCLUDED.balance
            ''', guild_id, user_id, reward)
        else:
            reward = await conn.fetchval('''
                SELECT points FROM review_point_rewards WHERE guild_id = $1 AND interaction_id = $2
            ''', guild_id, interaction_id)
        return reward, await get_balance(conn, guild_id, user_id)


async def _lock_order(conn, order_id, guild_id):
    order = await conn.fetchrow(
        'SELECT * FROM orders WHERE order_id = $1 AND guild_id = $2 FOR UPDATE', order_id, guild_id,
    )
    if order is None:
        raise PointsError("주문을 찾을 수 없습니다.")
    return dict(order)


async def request_payment(conn, order_id, guild_id, user_id, depositor_name, points):
    async with conn.transaction():
        order = await _lock_order(conn, order_id, guild_id)
        if order['buyer_id'] != user_id:
            raise PointsError("이 주문의 구매자만 결제할 수 있습니다.")
        if order['status'] != 'PENDING' or order['payment_requested']:
            raise PointsError("이미 결제를 요청했거나 처리된 주문입니다.")
        amount = parse_amount(order['amount'])
        balance = await _lock_balance(conn, guild_id, user_id)
        if not order['points_allowed']:
            points = 0
        elif points != 0 and not MIN_POINTS <= points <= MAX_POINTS:
            raise PointsError("포인트는 500~2,000P만 사용할 수 있습니다. 사용하지 않으려면 0을 입력하세요.")
        maximum = maximum_points(balance, amount, order['points_allowed'])
        if not 0 <= points <= maximum:
            raise PointsError(f"현재 사용할 수 있는 최대 포인트는 {maximum:,}P입니다. 결제 폼을 다시 열어 주세요.")
        if not depositor_name.strip():
            raise PointsError("입금자명을 입력해 주세요.")
        await conn.execute('''
            UPDATE point_balances SET balance = balance - $3 WHERE guild_id = $1 AND user_id = $2
        ''', guild_id, user_id, points)
        await conn.execute('''
            UPDATE orders SET depositor_name = $2, points_used = $3, cash_amount = $4,
                payment_requested = TRUE, points_refunded = FALSE WHERE order_id = $1
        ''', order_id, depositor_name.strip(), points, amount - points)
        order.update(depositor_name=depositor_name.strip(), points_used=points,
                     cash_amount=amount - points, payment_requested=True, points_refunded=False)
        return order


async def _refund_points(conn, order):
    if order['points_used'] and not order['points_refunded']:
        await _lock_balance(conn, order['guild_id'], order['buyer_id'])
        await conn.execute('''
            UPDATE point_balances SET balance = balance + $3 WHERE guild_id = $1 AND user_id = $2
        ''', order['guild_id'], order['buyer_id'], order['points_used'])
        await conn.execute('UPDATE orders SET points_refunded = TRUE WHERE order_id = $1', order['order_id'])


async def reset_failed_payment(conn, order_id, guild_id):
    async with conn.transaction():
        order = await _lock_order(conn, order_id, guild_id)
        if order['status'] != 'PENDING' or not order['payment_requested']:
            return
        await _refund_points(conn, order)
        await conn.execute('''
            UPDATE orders SET depositor_name = NULL, points_used = 0, cash_amount = NULL,
                payment_requested = FALSE, points_refunded = FALSE WHERE order_id = $1
        ''', order_id)


async def resolve_payment(conn, order_id, guild_id, approve):
    async with conn.transaction():
        order = await _lock_order(conn, order_id, guild_id)
        if order['status'] != 'PENDING':
            raise PointsError("이미 처리된 요청입니다.")
        if not order['payment_requested'] and not order['depositor_name']:
            raise PointsError("구매자의 결제 요청이 아직 없습니다.")
        await conn.execute('UPDATE orders SET status = $2 WHERE order_id = $1',
                           order_id, 'APPROVED' if approve else 'REJECTED')
        if approve:
            await conn.execute('''
                INSERT INTO user_info (user_id, total_spent) VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET total_spent = user_info.total_spent + $2
            ''', order['buyer_id'], parse_amount(order['amount']))
        else:
            await _refund_points(conn, order)
        return order


async def cancel_order_record(conn, order_id, guild_id):
    async with conn.transaction():
        order = await _lock_order(conn, order_id, guild_id)
        if order['status'] == 'APPROVED':
            await conn.execute('''
                UPDATE user_info SET total_spent = GREATEST(total_spent - $2, 0) WHERE user_id = $1
            ''', order['buyer_id'], parse_amount(order['amount']))
        await _refund_points(conn, order)
        await conn.execute('DELETE FROM orders WHERE order_id = $1', order_id)
        return order


def payment_summary(order):
    total = parse_amount(order['amount'])
    points = order.get('points_used') or 0
    cash = order.get('cash_amount')
    if cash is None:
        cash = total - points
    return (f"주문금액: `{total:,}원`\n"
            f"사용 포인트: `{points:,}P`\n"
            f"실제 입금금액: `{cash:,}원`")
