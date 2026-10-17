"""웹 구매후기의 Discord Components V2 메시지와 안내글 관리."""

from __future__ import annotations

import discord


WEB_REVIEW_BUYER_ROLE_ID = 1553595299560161381


def build_web_review_payload(rating: int, content: str, filenames=()) -> dict:
    filenames = [str(name) for name in filenames if str(name)]
    children = [
        {
            "type": 10,
            "content": (
                "## 구매후기\n"
                "**\n"
                f"- 구매자 : <@&{WEB_REVIEW_BUYER_ROLE_ID}>\n"
                "**"
            ),
        },
        {"type": 14, "divider": True},
        {"type": 10, "content": f"**{'⭐' * rating}**"},
        {"type": 14, "divider": True},
        {"type": 10, "content": f"**{content}**"},
    ]
    if filenames:
        children.extend([
            {"type": 14, "divider": True},
            {
                "type": 12,
                "items": [{"media": {"url": f"attachment://{filename}"}} for filename in filenames],
            },
        ])
    return {
        "flags": 1 << 15,
        "allowed_mentions": {"parse": [], "roles": [str(WEB_REVIEW_BUYER_ROLE_ID)]},
        "components": [{
            "type": 17,
            "accent_color": 0x32CD32,
            "components": children,
        }],
    }


async def replace_web_review_guide(conn, http, guild_id: int, channel_id: int, content: str):
    """웹 후기 등록 뒤 기존 안내를 지우고 Components V2 안내를 맨 아래에 둔다."""
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT (guild_id) DO NOTHING",
            guild_id,
        )
        settings = await conn.fetchrow(
            """SELECT review_channel_id,review_auto_message_id,review_auto_message_channel_id
               FROM guild_settings WHERE guild_id=$1 FOR UPDATE""",
            guild_id,
        )
        old_message_id = settings.get("review_auto_message_id") if settings else None
        old_channel_id = settings.get("review_auto_message_channel_id") if settings else None
        if old_message_id:
            delete_channel_id = old_channel_id or settings.get("review_channel_id") or channel_id
            try:
                await http.request(
                    discord.http.Route("DELETE", f"/channels/{int(delete_channel_id)}/messages/{int(old_message_id)}")
                )
            except discord.NotFound:
                pass

        payload = {
            "flags": 1 << 15,
            "allowed_mentions": {"parse": []},
            "components": [{
                "type": 17,
                "accent_color": 0x32CD32,
                "components": [{"type": 10, "content": content}],
            }],
        }
        response = await http.request(
            discord.http.Route("POST", f"/channels/{int(channel_id)}/messages"),
            json=payload,
        )
        if not response or not response.get("id"):
            raise RuntimeError("후기 안내 메시지 ID를 확인할 수 없습니다.")
        await conn.execute(
            """UPDATE guild_settings
               SET review_auto_message_id=$2,review_auto_message_channel_id=$3
               WHERE guild_id=$1""",
            guild_id, int(response["id"]), channel_id,
        )
        return int(response["id"])

