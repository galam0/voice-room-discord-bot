"""웹후크로 글쓴이 이름·프로필로 보내기"""

import discord

from core.config import log
from core.client import bot


# ─────────────── 쓴 사람 이름·프로필로 올리기 (웹후크) ───────────────
WEBHOOK_NAME = "통화방 봇"
_webhooks_by_channel: dict[int, discord.Webhook] = {}
_webhooks_by_id: dict[int, discord.Webhook] = {}


async def channel_webhook(channel):
    """채널에 봇이 만든 웹후크 (없으면 만들고, 권한이 없으면 None)."""
    if not isinstance(channel, discord.TextChannel):
        return None
    if channel.id in _webhooks_by_channel:
        return _webhooks_by_channel[channel.id]
    wh = None
    try:
        for w in await channel.webhooks():
            if w.user and w.user.id == bot.user.id and w.token:
                wh = w
                break
        if wh is None:
            wh = await channel.create_webhook(name=WEBHOOK_NAME, reason="구인·예약 글을 쓴 사람 이름으로 올리기")
    except discord.HTTPException:
        return None
    _webhooks_by_channel[channel.id] = wh
    _webhooks_by_id[wh.id] = wh
    return wh


async def webhook_by_id(webhook_id: int):
    if webhook_id not in _webhooks_by_id:
        try:
            _webhooks_by_id[webhook_id] = await bot.fetch_webhook(webhook_id)
        except discord.HTTPException:
            return None
    return _webhooks_by_id[webhook_id]


def forget_webhook(wh: discord.Webhook):
    _webhooks_by_id.pop(wh.id, None)
    for cid, w in list(_webhooks_by_channel.items()):
        if w.id == wh.id:
            del _webhooks_by_channel[cid]


async def post_as(member: discord.Member, channel, **kwargs):
    """명령어 친 사람 이름·프로필로 글 올리기. 웹후크를 못 쓰면 봇 이름으로. → (메시지, 웹후크ID)"""
    for _ in range(2):  # 기억해 둔 웹후크를 관리자가 지웠으면 새로 만들어서 한 번 더
        wh = await channel_webhook(channel)
        if wh is None:
            break
        try:
            msg = await wh.send(username=member.display_name[:80], avatar_url=member.display_avatar.url,
                                wait=True, **kwargs)
            return msg, wh.id
        except discord.NotFound:
            forget_webhook(wh)
        except discord.HTTPException as e:
            log.warning("웹후크로 못 올려서 봇 이름으로 올림: %s", e)
            break
    return await channel.send(**kwargs), None


async def edit_post(channel_id: int, message_id: int, webhook_id: int | None = None, **kwargs) -> bool:
    """올린 글 고치기 (웹후크 글이면 웹후크로). 글이 지워졌으면 False."""
    try:
        if webhook_id:
            wh = await webhook_by_id(webhook_id)
            if wh is None:
                return False
            await wh.edit_message(message_id, **kwargs)
        else:
            ch = bot.get_channel(channel_id)
            if ch is None:
                return False
            msg = await ch.fetch_message(message_id)
            await msg.edit(**kwargs)
        return True
    except discord.NotFound:
        return False
    except discord.HTTPException as e:
        log.warning("글 수정 실패 %s: %s", message_id, e)
        return True
