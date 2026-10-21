"""/구인, /구인마감, 구인 글 갱신·자동 마감"""

from datetime import datetime
import asyncio
import os
import time

from discord import app_commands
from discord.ext import tasks
import discord

from cards import recruit_card
from core.config import RECRUIT_EXPIRE_HOURS, TZ
from core.store import store
from core.client import bot
from core.utils import admin_log, existing_room, humans, mention_ok
from core.games import game_autocomplete, resolve_game, tier_autocomplete
from core.webhook import edit_post, post_as
from core.card_helpers import _applicant_entry, _avatar_bytes, custom_emoji_images


def expire_text(info: dict):
    if not info.get("expires_at"):
        return None
    t = datetime.fromtimestamp(info["expires_at"], TZ)
    ampm = "오전" if t.hour < 12 else "오후"
    hour = t.hour % 12 or 12
    return f"{ampm} {hour}:{t.minute:02d} 마감"


async def recruit_card_file(info: dict, voice, closed: bool = False) -> discord.File:
    """구인 글 이미지를 만들어 discord.File로 돌려줌."""
    members = humans(voice) if voice else []
    members.sort(key=lambda m: m.id != info["author"])  # 구인자가 맨 앞
    member_ids = {m.id for m in members}
    pending = [a for a in info.get("applicants", []) if a["id"] not in member_ids]
    avatars = await asyncio.gather(*[_avatar_bytes(m) for m in members[:recruit_card.MAX_SLOTS]])
    guild = bot.get_guild(info["guild"])
    room_left = max(recruit_card.MAX_SLOTS - len(members), 0)
    applicants = await asyncio.gather(*[_applicant_entry(guild, a) for a in pending[:room_left]]) if guild else []
    if closed:
        status = "closed"
    elif len(members) >= info["target"]:
        status = "full"
    else:
        status = "open"
    icon = None
    entry = store.game(info["guild"], info.get("game"))
    if entry and entry.get("file") and os.path.exists(entry["file"]):
        with open(entry["file"], "rb") as f:
            icon = f.read()
    buf = await asyncio.to_thread(
        recruit_card.render_card,
        icon=icon,
        title=info.get("game") or "구인",
        content=info["content"],
        author=info["author_name"],
        voice_name=voice.name if voice else info["voice_name"],
        target=info["target"],
        members=[(m.display_name, a) for m, a in zip(members, avatars)],
        applicants=list(applicants),
        tags=info.get("tags"),
        footer=None if closed else expire_text(info),
        custom_emojis=await custom_emoji_images(info["content"]),
        status=status,
    )
    return discord.File(buf, filename="recruit.png")


def recruit_view(info: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="참가", style=discord.ButtonStyle.primary,
                                    custom_id=f"recruit_apply:{info['voice']}"))
    view.add_item(discord.ui.Button(label="마감", style=discord.ButtonStyle.danger,
                                    custom_id=f"recruit_close:{info['voice']}"))
    return view


async def refresh_post(voice_id: int):
    """통화방 인원이 바뀌면 구인 글 이미지 갱신, 방이 비면 자동 마감."""
    info = store.post(voice_id)
    if not info:
        return
    voice = bot.get_channel(voice_id)
    if voice is None or not humans(voice):
        await close_post(voice_id)
        return
    ids = {m.id for m in voice.members}
    if any(a["id"] in ids for a in info.get("applicants", [])):
        info["applicants"] = [a for a in info["applicants"] if a["id"] not in ids]
        store.set_post(voice_id, **info)
    ok = await edit_post(info["channel"], info["message"], info.get("webhook"),
                         attachments=[await recruit_card_file(info, voice)], view=recruit_view(info))
    if not ok:
        store.remove_post(voice_id)


async def close_post(voice_id: int):
    info = store.post(voice_id)
    if not info:
        return
    store.remove_post(voice_id)
    voice = bot.get_channel(voice_id)
    await edit_post(info["channel"], info["message"], info.get("webhook"),
                    attachments=[await recruit_card_file(info, voice, closed=True)], view=None)


async def handle_recruit_close(interaction: discord.Interaction, voice_id: int):
    info = store.post(voice_id)
    if not info:
        await interaction.response.send_message("이미 마감된 구인이에요!", ephemeral=True)
        return
    is_admin = interaction.user.guild_permissions.manage_messages
    if interaction.user.id != info["author"] and not is_admin:
        await interaction.response.send_message("구인자만 마감할 수 있어요!", ephemeral=True)
        return
    await interaction.response.send_message("구인 마감!", ephemeral=True)
    await close_post(voice_id)


async def handle_recruit_apply(interaction: discord.Interaction, voice_id: int):
    """참가 버튼: 누르면 신청, 다시 누르면 취소."""
    info = store.post(voice_id)
    if not info:
        await interaction.response.send_message("이미 마감된 구인이에요!", ephemeral=True)
        return
    user = interaction.user
    voice = bot.get_channel(voice_id)
    if user.id == info["author"]:
        await interaction.response.send_message("구인자는 참가할 수 없어요!", ephemeral=True)
        return
    if voice and user in voice.members:
        await interaction.response.send_message("이미 통화방에 있어요!", ephemeral=True)
        return
    applicants = info.setdefault("applicants", [])
    if any(a["id"] == user.id for a in applicants):
        info["applicants"] = [a for a in applicants if a["id"] != user.id]
        store.set_post(voice_id, **info)
        await interaction.response.send_message("참가 취소!", ephemeral=True)
    else:
        applicants.append({"id": user.id, "name": user.display_name})
        store.set_post(voice_id, **info)
        await interaction.response.send_message(
            f"참가 완료! {voice.mention if voice else '통화방'}에 들어와 주세요. 들어오기 전까지는 예정으로 떠요.", ephemeral=True)
        if voice:
            try:
                await voice.send(f"{user.mention}님이 참가했어요! <@{info['author']}>",
                                 allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                pass
    await refresh_post(voice_id)


@tasks.loop(minutes=2)
async def expire_posts():
    now = time.time()
    for vid, info in list(store.data["posts"].items()):
        if info.get("expires_at") and info["expires_at"] <= now:
            await close_post(int(vid))


@bot.tree.command(name="구인", description="지금 있는 통화방으로 같이 할 사람을 구해요")
@app_commands.guild_only()
@app_commands.describe(
    내용="예: 경쟁 같이 하실 분, 마이크 필수",
    인원="나 포함 최대 몇 명으로 할지 (예: 5)",
    게임="등록된 게임 중에서 고르기 (게임 방이면 자동으로 채워져요)",
    티어="예: 골드, 골드~플래 (선택)",
    멘션역할="알림 보낼 역할 (게임에 연결된 역할은 자동으로 같이 멘션돼요)",
)
@app_commands.autocomplete(게임=game_autocomplete, 티어=tier_autocomplete)
async def cmd_recruit(interaction: discord.Interaction, 내용: app_commands.Range[str, 1, 300],
                      인원: app_commands.Range[int, 2, 20], 게임: str | None = None,
                      티어: app_commands.Range[str, 1, 30] | None = None,
                      멘션역할: discord.Role | None = None):
    member = interaction.user
    voice = member.voice.channel if member.voice and member.voice.channel else existing_room(interaction.guild, member)
    if voice is None:
        await interaction.response.send_message("통화방에 들어가 있어야 구인할 수 있어요!", ephemeral=True)
        return
    game = await resolve_game(interaction, 게임)
    if game is False:
        return
    if game is None and store.room(voice.id):  # 게임 방이면 그 게임으로
        game = store.room(voice.id).get("game")

    target_id = store.recruit_channel(interaction.guild.id)
    target = interaction.guild.get_channel(target_id) if target_id else interaction.channel
    if target is None or not hasattr(target, "send"):
        await interaction.response.send_message("구인 글을 올릴 채널이 없어요! 관리자에게 `/통화방설정 구인채널`을 해 달라고 하세요.", ephemeral=True)
        return

    current = len(humans(voice))
    if 인원 <= current:
        await interaction.response.send_message(
            f"{voice.mention}에 벌써 {current}명이 있어요! 인원은 {current + 1}명 이상으로 정해 주세요.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    # 같은 통화방의 이전 구인 글은 마감
    await close_post(voice.id)

    info = {"voice": voice.id, "voice_name": voice.name, "author": member.id, "guild": interaction.guild.id,
            "author_name": member.display_name, "content": 내용, "game": game, "target": 인원,
            "tags": [티어.strip()] if 티어 and 티어.strip() else [], "applicants": [],
            "expires_at": time.time() + RECRUIT_EXPIRE_HOURS * 3600 if RECRUIT_EXPIRE_HOURS > 0 else None}
    roles = [멘션역할] if 멘션역할 else []
    game_role_id = (store.game(interaction.guild.id, game) or {}).get("role")
    game_role = interaction.guild.get_role(game_role_id) if game_role_id else None
    if game_role and game_role not in roles:
        roles.append(game_role)
    header = " ".join(r.mention for r in roles) or None
    try:
        msg, webhook_id = await post_as(
            member, target,
            content=header,
            file=await recruit_card_file(info, voice),
            view=recruit_view(info),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
    except discord.HTTPException:
        await interaction.followup.send(f"{target.mention}에 글을 못 올렸어요! 봇 권한을 확인해 보세요.", ephemeral=True)
        return
    info.update(channel=target.id, message=msg.id, webhook=webhook_id)
    store.set_post(voice.id, **info)
    await admin_log(interaction.guild, f"📢 구인 · {member.mention} · {voice.mention} · {msg.jump_url}")
    done = f"구인 글 올렸어요! {msg.jump_url}"
    if roles and not mention_ok(interaction.guild, roles, target):
        done += "\n⚠️ 역할 멘션 알림이 안 갔을 수도 있어요. 관리자에게 그 역할의 '@멘션 허용'을 켜 달라고 하세요."
    await interaction.followup.send(done, ephemeral=True)


@bot.tree.command(name="구인마감", description="내가 올린 구인 글을 마감해요")
@app_commands.guild_only()
async def cmd_recruit_close(interaction: discord.Interaction):
    mine = [int(v) for v, p in store.data["posts"].items() if p["author"] == interaction.user.id]
    if not mine:
        await interaction.response.send_message("마감할 구인 글이 없어요!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    for vid in mine:
        await close_post(vid)
    await interaction.followup.send("구인 마감!", ephemeral=True)
