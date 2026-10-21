"""이미지 카드에 쓰는 프로필 사진·서버 이모지 받기"""

import asyncio
import re

import discord

from core.client import bot


async def _avatar_bytes(member: discord.Member):
    try:
        asset = member.display_avatar.replace(size=128, static_format="png")
        return await asyncio.wait_for(asset.read(), timeout=5)
    except Exception:
        return None


async def _applicant_entry(guild: discord.Guild, applicant: dict):
    member = guild.get_member(applicant["id"])
    if member is None:
        try:
            member = await guild.fetch_member(applicant["id"])
        except discord.HTTPException:
            return applicant["name"], None
    return member.display_name, await _avatar_bytes(member)


CUSTOM_EMOJI_RE = re.compile(r"<a?:\w{2,32}:(\d{15,25})>")
_custom_emoji_cache: dict[str, bytes | None] = {}


async def custom_emoji_images(text: str) -> dict:
    """내용에 들어 있는 서버 이모지 그림을 디스코드에서 받아 옴 (한 번 받으면 기억)."""
    out = {}
    for eid in set(CUSTOM_EMOJI_RE.findall(text or "")):
        if eid not in _custom_emoji_cache:
            try:
                _custom_emoji_cache[eid] = await asyncio.wait_for(
                    bot.http.get_from_cdn(f"https://cdn.discordapp.com/emojis/{eid}.png?size=96"), timeout=5)
            except Exception:
                _custom_emoji_cache[eid] = None
        if _custom_emoji_cache[eid]:
            out[eid] = _custom_emoji_cache[eid]
    return out
