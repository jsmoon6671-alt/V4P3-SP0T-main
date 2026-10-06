"""서버별 관리자 역할 설정과 채팅 시스템 제외 처리."""

import discord
from discord import app_commands


async def initialize_admin_roles_schema(conn):
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS guild_admin_roles (
            guild_id BIGINT NOT NULL,
            slot SMALLINT NOT NULL CHECK (slot BETWEEN 1 AND 5),
            role_id BIGINT NOT NULL,
            PRIMARY KEY (guild_id, slot),
            UNIQUE (guild_id, role_id)
        );
    ''')


async def get_admin_role_ids(conn, guild_id: int) -> set[int]:
    rows = await conn.fetch(
        'SELECT role_id FROM guild_admin_roles WHERE guild_id = $1 ORDER BY slot',
        guild_id,
    )
    return {int(row['role_id']) for row in rows}


def member_has_admin_role(member, role_ids: set[int]) -> bool:
    if not role_ids:
        return False
    return any(getattr(role, 'id', None) in role_ids for role in getattr(member, 'roles', ()))


def configured_admin_member_ids(guild, role_ids: set[int]) -> set[int]:
    member_ids = set()
    for role_id in role_ids:
        role = guild.get_role(role_id)
        if role:
            member_ids.update(member.id for member in role.members if not member.bot)
    return member_ids


async def save_admin_roles(conn, guild_id: int, role_ids: list[int]):
    async with conn.transaction():
        await conn.execute('DELETE FROM guild_admin_roles WHERE guild_id = $1', guild_id)
        for slot, role_id in enumerate(role_ids, start=1):
            await conn.execute(
                'INSERT INTO guild_admin_roles (guild_id, slot, role_id) VALUES ($1, $2, $3)',
                guild_id,
                slot,
                role_id,
            )


async def purge_admin_chat_point_activity(conn, guild_id: int, user_ids: set[int]):
    """관리자 역할 유저를 채팅 포인트 지급 후보에서 제거합니다."""
    if not user_ids:
        return
    values = sorted(user_ids)
    await conn.execute(
        'DELETE FROM chat_activity_messages WHERE guild_id = $1 AND user_id = ANY($2::bigint[])',
        guild_id,
        values,
    )
    await conn.execute(
        'DELETE FROM chat_activity_minutes WHERE guild_id = $1 AND user_id = ANY($2::bigint[])',
        guild_id,
        values,
    )
    await conn.execute(
        'DELETE FROM chat_activity_daily WHERE guild_id = $1 AND user_id = ANY($2::bigint[])',
        guild_id,
        values,
    )


async def purge_admin_rank_activity(conn, guild_id: int, user_ids: set[int]):
    """관리자 역할 유저의 채팅 순위 기록을 제거합니다."""
    if not user_ids:
        return
    await conn.execute(
        'DELETE FROM chat_rank_scores WHERE guild_id = $1 AND user_id = ANY($2::bigint[])',
        guild_id,
        sorted(user_ids),
    )


async def purge_admin_chat_activity(conn, guild_id: int, user_ids: set[int]):
    await purge_admin_chat_point_activity(conn, guild_id, user_ids)
    await purge_admin_rank_activity(conn, guild_id, user_ids)


def register_admin_role_command(bot):
    @bot.tree.command(name='관리자', description='관리자 역할을 최대 5개 설정합니다. (서버 관리자 전용)')
    @app_commands.describe(
        역할1='첫 번째 관리자 역할 (필수)',
        역할2='두 번째 관리자 역할',
        역할3='세 번째 관리자 역할',
        역할4='네 번째 관리자 역할',
        역할5='다섯 번째 관리자 역할',
    )
    async def configure_admin_roles(
        interaction: discord.Interaction,
        역할1: discord.Role,
        역할2: discord.Role | None = None,
        역할3: discord.Role | None = None,
        역할4: discord.Role | None = None,
        역할5: discord.Role | None = None,
    ):
        permissions = getattr(interaction.user, 'guild_permissions', None)
        if not permissions or not permissions.administrator:
            await interaction.response.send_message(
                '❌ `/관리자` 설정은 Discord 서버 관리자만 변경할 수 있습니다.',
                ephemeral=True,
            )
            return
        if bot.db_pool is None:
            await interaction.response.send_message('❌ 데이터베이스가 연결되지 않았습니다.', ephemeral=True)
            return

        roles = [role for role in (역할1, 역할2, 역할3, 역할4, 역할5) if role is not None]
        if any(role.is_default() for role in roles):
            await interaction.response.send_message('❌ `@everyone` 역할은 관리자 역할로 설정할 수 없습니다.', ephemeral=True)
            return
        if len({role.id for role in roles}) != len(roles):
            await interaction.response.send_message('❌ 같은 역할을 중복해서 설정할 수 없습니다.', ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        role_ids = [role.id for role in roles]
        excluded_ids = configured_admin_member_ids(interaction.guild, set(role_ids))
        async with bot.db_pool.acquire() as conn:
            await save_admin_roles(conn, interaction.guild.id, role_ids)
            await purge_admin_chat_activity(conn, interaction.guild.id, excluded_ids)

        role_text = '\n'.join(f'- {role.mention}' for role in roles)
        await interaction.followup.send(
            '✅ 관리자 역할을 설정했습니다.\n\n'
            f'{role_text}\n\n'
            '해당 역할의 유저는 채팅 포인트 추첨과 채팅 순위 집계에서 제외됩니다.\n'
            '-# 기존 공용 포인트 잔액은 구매·후기·게임 포인트와 함께 사용되므로 차감하지 않습니다.',
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    return configure_admin_roles
