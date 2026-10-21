"""/예약 (예약 대기, 10분 전 알림 + 방 자동 생성)"""

from datetime import datetime
from datetime import timedelta
import asyncio
import os
import time

from discord import app_commands
from discord.ext import tasks
import discord

from cards import recruit_card
from core.config import TZ, log
from core.store import store
from core.client import bot
from core.utils import admin_log, existing_room, humans, mention_ok, user_lock
from core.games import game_autocomplete, resolve_game, tier_autocomplete
from core.webhook import edit_post, post_as
from core.card_helpers import _applicant_entry, custom_emoji_images
from core.timeparse import parse_time
from cogs.rooms import create_room, pick_category, schedule_empty_check


# ─────────────────────────────── /예약 ───────────────────────────────
REMIND_BEFORE = 600  # 시작 10분 전에 알림 + 통화방 생성
WEEKDAYS = "월화수목금토일"


def time_text(ts: float) -> str:
    t = datetime.fromtimestamp(ts, TZ)
    days = (t.date() - datetime.now(TZ).date()).days
    day = {0: "오늘", 1: "내일", 2: "모레"}.get(days, f"{t.month}월 {t.day}일 ({WEEKDAYS[t.weekday()]})")
    ampm = "오전" if t.hour < 12 else "오후"
    return f"{day} {ampm} {t.hour % 12 or 12}:{t.minute:02d}"


async def res_card_file(res: dict, status: str) -> discord.File:
    guild = bot.get_guild(res["guild"])
    parts = sorted(res["participants"], key=lambda p: p["id"] != res["author"])  # 주최자가 맨 앞
    room = bot.get_channel(res["room"]) if res.get("room") and status in ("open", "full") else None
    if room:  # 통화방이 열린 뒤: 들어온 사람은 진하게, 아직 안 온 사람은 흐리게
        present = {m.id for m in humans(room)}
        here = [p for p in parts if p["id"] in present]
        away = [p for p in parts if p["id"] not in present]
    else:
        here, away = parts, []
    here = here[:recruit_card.MAX_SLOTS]
    away = away[:max(recruit_card.MAX_SLOTS - len(here), 0)]
    if guild:
        members = await asyncio.gather(*[_applicant_entry(guild, p) for p in here])
        pending = await asyncio.gather(*[_applicant_entry(guild, p) for p in away])
    else:
        members, pending = [(p["name"], None) for p in here], [(p["name"], None) for p in away]
    icon = None
    entry = store.game(res["guild"], res.get("game"))
    if entry and entry.get("file") and os.path.exists(entry["file"]):
        with open(entry["file"], "rb") as f:
            icon = f.read()
    footer = None
    buf = await asyncio.to_thread(
        recruit_card.render_card,
        icon=icon,
        title=res.get("game") or "예약",
        content=res["content"],
        author=res["author_name"],
        voice_name="",
        target=res["target"],
        members=list(members),
        applicants=list(pending),
        remaining=res["target"] - len(res["participants"]),
        count_pending=True,
        status=status,
        tags=[f"{time_text(res['start_at'])} 시작"] + res.get("tags", [])
             + ([f"대기 {len(res['waitlist'])}명"] if res.get("waitlist") else []),
        meta=f"주최 {res['author_name']}",
        footer=footer,
        custom_emojis=await custom_emoji_images(res["content"]),
    )
    return discord.File(buf, filename="reservation.png")


def res_by_room(room_id: int):
    for mid, res in store.data["reservations"].items():
        if res.get("room") == room_id:
            return mid, res
    return None


def res_status(res: dict) -> str:
    return "full" if len(res["participants"]) >= res["target"] else "open"


def res_view(message_id: int, res: dict, finished: bool = False) -> discord.ui.View | None:
    view = discord.ui.View(timeout=None)
    room = bot.get_channel(res["room"]) if res.get("room") else None
    if room:
        view.add_item(discord.ui.Button(label="🔊 통화방 들어가기", style=discord.ButtonStyle.link, url=room.jump_url))
    if not finished:
        view.add_item(discord.ui.Button(label="참가", style=discord.ButtonStyle.primary, custom_id=f"res_join:{message_id}"))
        view.add_item(discord.ui.Button(label="취소", style=discord.ButtonStyle.danger, custom_id=f"res_cancel:{message_id}"))
    return view if view.children else None


async def update_res_post(message_id: int, res: dict, status: str | None = None, msg=None):
    status = status or res_status(res)
    finished = status in ("started", "cancelled")
    await edit_post(res["channel"], message_id, res.get("webhook"),
                    attachments=[await res_card_file(res, status)], view=res_view(message_id, res, finished))


async def handle_res_join(interaction: discord.Interaction, message_id: int):
    res = store.res(message_id)
    if not res:
        await interaction.response.send_message("이미 끝났거나 취소된 예약이에요.", ephemeral=True)
        return
    user = interaction.user
    if user.id == res["author"]:
        await interaction.response.send_message("주최자는 이미 참가돼 있어요.",
                                                ephemeral=True)
        return
    await interaction.response.defer()
    waiting = res.setdefault("waitlist", [])
    promoted = None
    if any(p["id"] == user.id for p in res["participants"]):
        res["participants"] = [p for p in res["participants"] if p["id"] != user.id]
        if waiting:  # 빈자리는 대기 1번에게
            promoted = waiting.pop(0)
            res["participants"].append(promoted)
        note = "참가를 취소했어요."
    elif any(p["id"] == user.id for p in waiting):
        res["waitlist"] = [p for p in waiting if p["id"] != user.id]
        note = "대기를 취소했어요."
    elif len(res["participants"]) >= res["target"]:
        waiting.append({"id": user.id, "name": user.display_name})
        note = f"자리가 다 차서 대기 {len(waiting)}번이에요."
    else:
        res["participants"].append({"id": user.id, "name": user.display_name})
        note = "참가했어요."
    store.set_res(message_id, res)
    await update_res_post(message_id, res, msg=interaction.message)
    await interaction.followup.send(note, ephemeral=True)
    if promoted:
        try:
            await interaction.channel.send(f"<@{promoted['id']}> 대기에서 참가로 넘어왔어요.",
                                           allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            pass


async def handle_res_cancel(interaction: discord.Interaction, message_id: int):
    res = store.res(message_id)
    if not res:
        await interaction.response.send_message("이미 끝났거나 취소된 예약이에요.", ephemeral=True)
        return
    if interaction.user.id != res["author"] and not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("예약한 사람만 취소할 수 있어요.",
                                                ephemeral=True)
        return
    await interaction.response.defer()
    store.remove_res(message_id)
    await update_res_post(message_id, res, status="cancelled", msg=interaction.message)
    await admin_log(interaction.guild, f"📅 예약 취소 · {interaction.user.mention} · {interaction.message.jump_url}")
    await interaction.followup.send("예약을 취소했어요.", ephemeral=True)


async def remind_res(message_id: int, res: dict):
    """시작 10분 전: 통화방 만들고 참가자 멘션."""
    guild = bot.get_guild(res["guild"])
    channel = bot.get_channel(res["channel"])
    if guild is None or channel is None:
        return
    room = None
    try:
        host = guild.get_member(res["author"]) or await guild.fetch_member(res["author"])
        async with user_lock(host.id):
            room = existing_room(guild, host)
            if room is None:
                room = await create_room(host, pick_category(guild, host, channel),
                                         limit=min(res["target"], 99), game=res.get("game"))
                # 시작 15분 뒤까지 아무도 없으면 정리
                asyncio.create_task(schedule_empty_check(room.id, delay=res["start_at"] - time.time() + 900))
    except discord.HTTPException as e:
        log.warning("예약 통화방 생성 실패: %s", e)
    res["room"] = room.id if room else None
    store.set_res(message_id, res)
    mentions = " ".join(f"<@{p['id']}>" for p in res["participants"])
    where = room.mention if room else ""
    view = discord.ui.View()
    if room:
        view.add_item(discord.ui.Button(label="🔊 통화방 들어가기", style=discord.ButtonStyle.link, url=room.jump_url))
    try:
        await channel.send(f"⏰ {res.get('game') or '예약'} 10분 후 시작 {where}\n{mentions}",
                           view=view if room else discord.utils.MISSING,
                           allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
    except discord.HTTPException as e:
        log.warning("예약 알림 실패: %s", e)
    await update_res_post(message_id, res)


@tasks.loop(seconds=30)
async def reservation_tick():
    now = time.time()
    for mid, res in list(store.data["reservations"].items()):
        try:
            if now - res["start_at"] > 1800:  # 봇이 꺼져 있던 사이에 지나간 예약
                store.remove_res(mid)
                await update_res_post(int(mid), res, status="started")
                continue
            if not res.get("reminded") and now >= res["start_at"] - REMIND_BEFORE:
                res["reminded"] = True
                store.set_res(mid, res)
                await remind_res(int(mid), res)
            if now >= res["start_at"]:
                store.remove_res(mid)
                await update_res_post(int(mid), res, status="started")
        except Exception:
            log.exception("예약 처리 실패 %s", mid)


@bot.tree.command(name="예약", description="시간을 정해서 같이 할 사람을 모아요 (10분 전에 알림 + 통화방 자동 생성)")
@app_commands.guild_only()
@app_commands.describe(
    시간="예: 21:00, 오후 9시, 내일 20:30, 10/25 21:00",
    인원="나 포함 최대 몇 명 (예: 내전이면 10)",
    게임="등록된 게임 중에서 고르기",
    내용="예: 5:5 내전, 늦지 말기 (선택)",
    티어="예: 골드~플래 (선택)",
    멘션역할="알림 보낼 역할 (게임에 연결된 역할은 자동으로 같이 멘션돼요)",
)
@app_commands.autocomplete(게임=game_autocomplete, 티어=tier_autocomplete)
async def cmd_reserve(interaction: discord.Interaction, 시간: app_commands.Range[str, 1, 30],
                      인원: app_commands.Range[int, 2, 20], 게임: str | None = None,
                      내용: app_commands.Range[str, 1, 200] | None = None,
                      티어: app_commands.Range[str, 1, 30] | None = None,
                      멘션역할: discord.Role | None = None):
    member = interaction.user
    game = await resolve_game(interaction, 게임)
    if game is False:
        return
    if game is None and member.voice and member.voice.channel and store.room(member.voice.channel.id):
        game = store.room(member.voice.channel.id).get("game")
    now = datetime.now(TZ)
    start = parse_time(시간, now)
    if start is None:
        await interaction.response.send_message(
            "시간을 읽지 못했어요. `21:00`, `오후 9시`, `내일 20:30`, `10/25 21:00`처럼 적어 주세요.", ephemeral=True)
        return
    if start - now > timedelta(days=7):
        await interaction.response.send_message("예약은 7일 안쪽으로만 할 수 있어요.", ephemeral=True)
        return
    target_id = store.recruit_channel(interaction.guild.id)
    target = interaction.guild.get_channel(target_id) if target_id else interaction.channel
    if target is None or not hasattr(target, "send"):
        await interaction.response.send_message("글을 올릴 채널을 찾지 못했어요. 관리자에게 `/통화방설정 구인채널`을 부탁해 주세요.",
                                                ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    res = {
        "guild": interaction.guild.id, "channel": target.id, "author": member.id, "author_name": member.display_name,
        "game": game, "content": 내용 or "같이 할 사람 모여요!", "target": 인원, "start_at": start.timestamp(),
        "tags": [티어.strip()] if 티어 and 티어.strip() else [], "participants": [{"id": member.id, "name": member.display_name}],
        "reminded": False, "room": None,
    }
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
            file=await res_card_file(res, res_status(res)),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
    except discord.HTTPException:
        await interaction.followup.send(f"{target.mention}에 글을 올리지 못했어요. 봇 권한을 확인해 주세요.", ephemeral=True)
        return
    res["webhook"] = webhook_id
    # 버튼에 메시지 ID가 들어가야 해서 올린 뒤에 붙임
    await edit_post(target.id, msg.id, webhook_id, view=res_view(msg.id, res))
    store.set_res(msg.id, res)
    await admin_log(interaction.guild, f"📅 예약 · {member.mention} · {time_text(res['start_at'])} · {msg.jump_url}")
    done = f"예약했어요: {msg.jump_url} ({time_text(res['start_at'])})"
    if roles and not mention_ok(interaction.guild, roles, target):
        done += "\n⚠️ 역할 멘션이 알림으로 가지 않았을 수 있어요. 관리자에게 그 역할의 '@멘션 허용' 설정을 부탁해 주세요."
    await interaction.followup.send(done, ephemeral=True)
