"""여러 기능에 걸친 디스코드 이벤트 (봇 시작, 음성 입퇴장, 버튼, 채널 삭제, 명령어 기록·오류)"""

import asyncio

from discord import app_commands
import discord

from core.config import log
from core.store import store
from core.client import bot
from core.utils import admin_log, existing_room, humans, user_lock
from cogs.recruit import close_post, expire_posts, handle_recruit_apply, handle_recruit_close, refresh_post
from cogs.rooms import (create_room, delete_room, handle_panel, hold_left, hub_room_category,
                        schedule_empty_check, transfer_owner)
from cogs.reservation import handle_res_cancel, handle_res_join, res_by_room, reservation_tick, update_res_post
from cogs.giveaway import giveaway_tick
from cogs.help import COMMAND_IDS


# ─────────────────────────────── 이벤트 ───────────────────────────────
@bot.event
async def on_ready():
    if not expire_posts.is_running():
        expire_posts.start()
    if not reservation_tick.is_running():
        reservation_tick.start()
    if not giveaway_tick.is_running():
        giveaway_tick.start()
    # 꺼져 있는 동안 비거나 사라진 방 정리
    for cid in list(store.data["rooms"].keys()):
        ch = bot.get_channel(int(cid))
        if ch is None:
            store.remove_room(int(cid))
        elif not humans(ch):
            if hold_left(ch.id):  # 예약 통화방은 시작 15분 뒤까지 둠
                asyncio.create_task(schedule_empty_check(ch.id, delay=hold_left(ch.id)))
            else:
                await delete_room(ch, "봇 재시작 후 빈 통화방 정리")
    for vid in list(store.data["posts"].keys()):
        await refresh_post(int(vid))
    if not COMMAND_IDS:  # 연결이 끊겼다 다시 붙을 때는 명령어를 다시 올리지 않음
        synced = await bot.tree.sync()
        COMMAND_IDS.update({c.name: c.id for c in synced})
        log.info("로그인: %s / 명령어 %d개 동기화", bot.user, len(synced))


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot or before.channel == after.channel:
        return
    guild = member.guild

    # 1) 생성 채널에 들어옴 → 방 만들고 이동
    hub = store.hubs(guild.id).get(str(after.channel.id)) if after.channel else None
    if hub is not None:
        async with user_lock(member.id):
            room = existing_room(guild, member)
            created = room is None
            if created:
                try:
                    room = await create_room(member, hub_room_category(guild, after.channel, hub),
                                             limit=hub.get("limit", 0), game=hub.get("game"))
                except discord.HTTPException as e:
                    log.warning("방 생성 실패: %s", e)
                    return
            try:
                await member.move_to(room, reason="통화방 자동 이동")
            except discord.HTTPException:
                # 그 사이에 나가버린 경우
                if created and not humans(room):
                    await delete_room(room, "방장이 바로 나감")

    # 2) 봇이 만든 방에서 나감 → 비었으면 삭제, 방장이면 위임
    if before.channel and store.room(before.channel.id):
        channel = before.channel
        room = store.room(channel.id)
        left = humans(channel)
        if not left:
            if hold_left(channel.id):  # 예약 통화방은 시작 15분 뒤까지 둠
                asyncio.create_task(schedule_empty_check(channel.id, delay=hold_left(channel.id)))
            else:
                await delete_room(channel, "빈 통화방")
        elif room["owner"] == member.id:
            # 이미 다른 방을 가진 사람은 피해서 위임
            candidates = [m for m in left if store.room_of_owner(guild.id, m.id) is None] or left
            await transfer_owner(channel, candidates[0])

    # 3) 구인 글이 걸린 채널이면 인원 갱신 (봇이 안 만든 일반 음성 채널도 포함)
    for ch in {before.channel, after.channel}:
        if ch and store.post(ch.id):
            await refresh_post(ch.id)

    # 4) 예약 통화방: 참가 안 누르고 들어와도 자리가 있으면 참가자로 추가, 들어오고 나갈 때마다 글 갱신
    for ch in {before.channel, after.channel}:
        found = res_by_room(ch.id) if ch else None
        if not found:
            continue
        mid, res = found
        if ch == after.channel and all(p["id"] != member.id for p in res["participants"]) \
                and len(res["participants"]) < res["target"]:
            res["participants"].append({"id": member.id, "name": member.display_name})
            store.set_res(mid, res)
        await update_res_post(int(mid), res)


@bot.event
async def on_interaction(interaction: discord.Interaction):
    """패널·구인·예약 버튼 (봇이 재시작돼도 동작하도록 custom_id로 처리)."""
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = (interaction.data or {}).get("custom_id", "")
    try:
        if custom_id.startswith("res_join:"):
            await handle_res_join(interaction, int(custom_id.split(":")[1]))
        elif custom_id.startswith("res_cancel:"):
            await handle_res_cancel(interaction, int(custom_id.split(":")[1]))
        elif custom_id.startswith("recruit_apply:"):
            await handle_recruit_apply(interaction, int(custom_id.split(":")[1]))
        elif custom_id.startswith("recruit_close:"):
            await handle_recruit_close(interaction, int(custom_id.split(":")[1]))
        elif custom_id.startswith("panel:"):
            _, action, cid = custom_id.split(":")
            await handle_panel(interaction, action, int(cid))
    except discord.HTTPException as e:
        log.warning("버튼 처리 실패 %s: %s", custom_id, e)
        if not interaction.response.is_done():
            await interaction.response.send_message("실패했어요! 봇 권한을 확인해 보세요.", ephemeral=True)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    store.remove_menu(payload.message_id)
    store.remove_res(payload.message_id)
    if store.data["giveaways"].pop(str(payload.message_id), None) is not None:
        store.save()


@bot.event
async def on_guild_channel_delete(channel):
    store.remove_room(channel.id)
    await close_post(channel.id)
    store.remove_hub(channel.guild.id, channel.id)
    if store.recruit_channel(channel.guild.id) == channel.id:
        store.set_recruit_channel(channel.guild.id, None)
    if store.room_category(channel.guild.id) == channel.id:
        store.set_room_category(channel.guild.id, None)
    for hub in store.hubs(channel.guild.id).values():
        if hub.get("category") == channel.id:
            hub["category"] = None
            store.save()


LOGGED_GROUPS = ("통화방설정", "역할메뉴", "자동역할", "추첨", "인증")


@bot.event
async def on_app_command_completion(interaction: discord.Interaction, command):
    if interaction.guild and command.qualified_name.split()[0] in LOGGED_GROUPS:
        opts = " ".join(f"{k}:{getattr(v, 'name', getattr(v, 'filename', v))}" for k, v in interaction.namespace)
        await admin_log(interaction.guild, f"⚙️ {interaction.user.mention} · `/{command.qualified_name}` {opts}".strip())


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("명령어 오류", exc_info=error)
    msg = "오류가 났어요! 봇 권한(채널 관리·역할 관리·멤버 이동)을 확인해 보세요."
    if isinstance(error, app_commands.MissingPermissions):
        msg = "관리자만 쓸 수 있는 명령어예요!"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass
