"""여러 기능이 같이 쓰는 도우미 함수"""

import asyncio

import discord

from core.config import RENAME_TIMEOUT
from core.store import store


# 같은 사람이 동시에 방을 두 개 만드는 걸 막는 잠금
_user_locks: dict[int, asyncio.Lock] = {}


def user_lock(user_id: int) -> asyncio.Lock:
    return _user_locks.setdefault(user_id, asyncio.Lock())


# ─────────────────────────────── 도우미 ───────────────────────────────
def default_name(member: discord.Member, game: str | None = None) -> str:
    return f"{member.display_name}의 {game or '통화방'}"[:100]


def humans(channel):
    return [m for m in channel.members if not m.bot]


async def try_rename(channel: discord.VoiceChannel, name: str) -> bool:
    """이름 변경. 디스코드 제한(10분에 2번)에 걸리면 기다리지 않고 False."""
    try:
        await asyncio.wait_for(channel.edit(name=name[:100]), timeout=RENAME_TIMEOUT)
        return True
    except (asyncio.TimeoutError, discord.HTTPException):
        return False


async def notify(channel: discord.VoiceChannel, text: str):
    """음성 채널의 채팅에 안내 메시지 (실패해도 무시)."""
    try:
        await channel.send(text)
    except discord.HTTPException:
        pass


async def admin_log(guild, text: str):
    """관리자 로그 채널에 한 줄 기록 (채널이 안 정해져 있으면 아무것도 안 함)."""
    if guild is None:
        return
    cid = store.guild(guild.id).get("log")
    ch = guild.get_channel(cid) if cid else None
    if ch is None:
        return
    try:
        await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass


def owner_tag(channel) -> str:
    room = store.room(channel.id)
    return f"<@{room['owner']}>" if room else "?"


def existing_room(guild: discord.Guild, member: discord.Member):
    """이미 가진 방이 있으면 반환 (채널이 사라졌으면 기록 정리)."""
    cid = store.room_of_owner(guild.id, member.id)
    if cid is None:
        return None
    ch = guild.get_channel(cid)
    if ch is None:
        store.remove_room(cid)
    return ch


def mention_ok(guild: discord.Guild, roles, channel=None) -> bool:
    """봇이 이 역할들을 실제로 멘션(알림)할 수 있는지."""
    perms = channel.permissions_for(guild.me) if channel else guild.me.guild_permissions
    return perms.mention_everyone or all(r.mentionable for r in roles)
