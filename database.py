"""봇의 기본 테이블과 기능 모듈별 스키마 초기화."""

from admin_roles import initialize_admin_roles_schema
from chat_points import initialize_chat_points_schema
from chat_ranking import initialize_chat_ranking_schema
from join_applications import initialize_join_application_schema
from loyalty_points import initialize_points_schema


async def initialize_database(conn):
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id BIGINT PRIMARY KEY,
            approval_channel_id BIGINT,
            log_channel_id BIGINT,
            ticket_cat_purchase BIGINT,
            ticket_cat_general BIGINT,
            ticket_cat_partner BIGINT,
            archive_cat_purchase BIGINT,
            archive_cat_general BIGINT,
            archive_cat_partner BIGINT,
            ticket_log_channel_id BIGINT,
            tier_log_channel_id BIGINT,
            buyer_role_id BIGINT,
            review_channel_id BIGINT,
            leaderboard_channel_id BIGINT,
            leaderboard_message_id BIGINT,
            clock_in_vc BIGINT,
            clock_out_vc BIGINT,
            clock_log_channel BIGINT,
            bank_name TEXT,
            account_number TEXT,
            account_holder TEXT,
            pop_device_channel_id BIGINT,
            pop_device_message_id BIGINT,
            pop_liquid_channel_id BIGINT,
            pop_liquid_message_id BIGINT,
            buyer_info_channel_id BIGINT,
            review_auto_message TEXT,
            review_auto_message_id BIGINT,
            review_auto_message_channel_id BIGINT,
            leaderboard_first_role_id BIGINT,
            leaderboard_first_user_id BIGINT,
            anonymous_role_id BIGINT
        );
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            order_id VARCHAR(50) PRIMARY KEY,
            guild_id BIGINT,
            original_channel_id BIGINT,
            buyer_id BIGINT,
            product TEXT,
            quantity TEXT,
            amount TEXT,
            status VARCHAR(20),
            is_anonymous BOOLEAN DEFAULT FALSE,
            depositor_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS user_info (
            user_id BIGINT PRIMARY KEY,
            name TEXT,
            contact TEXT,
            address TEXT,
            cvs TEXT,
            is_anonymous BOOLEAN DEFAULT FALSE,
            total_spent BIGINT DEFAULT 0
        );
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS tickets (
            channel_id BIGINT PRIMARY KEY,
            user_id BIGINT,
            ticket_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS vip_tiers (
            guild_id BIGINT,
            role_id BIGINT,
            required_amount BIGINT,
            PRIMARY KEY (guild_id, role_id)
        );
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS price_lists (
            guild_id BIGINT,
            type VARCHAR(20),
            name TEXT,
            price TEXT,
            options TEXT,
            image_url TEXT,
            PRIMARY KEY (guild_id, name)
        );
    ''')

    updates = [
        'ALTER TABLE user_info ADD COLUMN is_anonymous BOOLEAN DEFAULT FALSE;',
        'ALTER TABLE user_info ADD COLUMN total_spent BIGINT DEFAULT 0;',
        'ALTER TABLE orders ADD COLUMN is_anonymous BOOLEAN DEFAULT FALSE;',
        'ALTER TABLE orders ADD COLUMN depositor_name TEXT;',
        'ALTER TABLE orders ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;',
        'ALTER TABLE guild_settings ADD COLUMN ticket_cat_purchase BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN ticket_cat_general BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN ticket_cat_partner BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN archive_cat_purchase BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN archive_cat_general BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN archive_cat_partner BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN ticket_log_channel_id BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN tier_log_channel_id BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN buyer_role_id BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN review_channel_id BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN leaderboard_channel_id BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN leaderboard_message_id BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN clock_in_vc BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN clock_out_vc BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN clock_log_channel BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN bank_name TEXT;',
        'ALTER TABLE guild_settings ADD COLUMN account_number TEXT;',
        'ALTER TABLE guild_settings ADD COLUMN account_holder TEXT;',
        'ALTER TABLE guild_settings ADD COLUMN pop_device_channel_id BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN pop_device_message_id BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN pop_liquid_channel_id BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN pop_liquid_message_id BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN buyer_info_channel_id BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN review_auto_message TEXT;',
        'ALTER TABLE guild_settings ADD COLUMN review_auto_message_id BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN review_auto_message_channel_id BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN leaderboard_first_role_id BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN leaderboard_first_user_id BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN anonymous_role_id BIGINT;',
        'ALTER TABLE guild_settings ADD COLUMN join_log_channel_id BIGINT;',
    ]
    for query in updates:
        try:
            await conn.execute(query)
        except Exception:
            # PostgreSQL 구버전 호환을 위해 이미 존재하는 컬럼 오류는 무시합니다.
            pass

    await initialize_points_schema(conn)
    await initialize_chat_points_schema(conn)
    await initialize_join_application_schema(conn)
    await initialize_admin_roles_schema(conn)
    await initialize_chat_ranking_schema(conn)
