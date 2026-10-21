"""/통화방설정 (관리자)"""

import asyncio
import hashlib
import io
import os
import shutil

from discord import app_commands
import discord

from core.config import ICON_DIR
from core.store import game_key, store
from core.client import bot
from core.utils import mention_ok
from core.games import game_autocomplete, resolve_game
from cogs.rooms import first_hub_category


# ─────────────────────────────── /통화방설정 (관리자) ───────────────────────────────
setup_group = app_commands.Group(
    name="통화방설정", description="통화방 봇 설정 (관리자)", guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@setup_group.command(name="만들기", description="'통화방' 카테고리와 '➕ 통화방 만들기' 채널을 새로 만들어요")
async def cmd_setup_make(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    category = first_hub_category(guild) or await guild.create_category("통화방", reason="통화방 봇 설정")
    hub = await guild.create_voice_channel("➕ 통화방 만들기", category=category, reason="통화방 봇 설정")
    store.set_hub(guild.id, hub.id)
    await interaction.followup.send(f"{hub.mention} 만들었어요!", ephemeral=True)


@setup_group.command(name="게임채널", description="게임별 생성 채널을 만들어요 (예: ➕ 발로란트 5인방)")
@app_commands.describe(게임="등록된 게임", 인원="만들어질 방 인원 제한 (0 = 제한 없음)", 채널이름="비우면 '➕ 게임 N인방'")
@app_commands.autocomplete(게임=game_autocomplete)
async def cmd_setup_game_hub(interaction: discord.Interaction, 게임: str, 인원: app_commands.Range[int, 0, 99],
                             채널이름: app_commands.Range[str, 1, 100] | None = None):
    game = await resolve_game(interaction, 게임)
    if not game:
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    category = first_hub_category(guild) or await guild.create_category("통화방", reason="통화방 봇 설정")
    name = 채널이름 or (f"➕ {game} {인원}인방" if 인원 else f"➕ {game}")
    hub = await guild.create_voice_channel(name[:100], category=category, reason="통화방 봇 게임 채널")
    store.set_hub(guild.id, hub.id, game=game, limit=인원)
    limit_text = f"인원 {인원}명 제한" if 인원 else "인원 제한 없음"
    await interaction.followup.send(f"{hub.mention} 만들었어요! ({limit_text})",
                                    ephemeral=True)


@setup_group.command(name="지정", description="이미 있는 음성 채널을 생성 채널로 지정해요 (게임 지정 가능)")
@app_commands.describe(채널="생성 채널로 쓸 음성 채널", 게임="등록된 게임 (선택)", 인원="만들어질 방 인원 제한")
@app_commands.autocomplete(게임=game_autocomplete)
async def cmd_setup_set(interaction: discord.Interaction, 채널: discord.VoiceChannel, 게임: str | None = None,
                        인원: app_commands.Range[int, 0, 99] = 0):
    game = await resolve_game(interaction, 게임)
    if game is False:
        return
    store.set_hub(interaction.guild.id, 채널.id, game=game, limit=인원)
    await interaction.response.send_message(f"{채널.mention}을(를) 생성 채널로 지정했어요!",
                                            ephemeral=True)


@setup_group.command(name="해제", description="생성 채널 지정을 해제해요 (채널은 지우지 않아요)")
async def cmd_setup_clear(interaction: discord.Interaction, 채널: discord.VoiceChannel):
    if store.remove_hub(interaction.guild.id, 채널.id):
        await interaction.response.send_message(f"{채널.mention} 생성 채널 해제!", ephemeral=True)
    else:
        await interaction.response.send_message(f"{채널.mention}은(는) 생성 채널이 아니에요!", ephemeral=True)


@setup_group.command(name="목록", description="생성 채널·등록된 게임·구인 채널을 보여줘요")
async def cmd_setup_list(interaction: discord.Interaction):
    gid = interaction.guild.id
    hub_lines = []
    for cid, cfg in store.hubs(gid).items():
        detail = cfg.get("game") or "일반"
        if cfg.get("limit"):
            detail += f", {cfg['limit']}명"
        if cfg.get("category"):
            detail += f", 📁 <#{cfg['category']}>"
        hub_lines.append(f"<#{cid}> ({detail})")
    def game_line(e):
        line = e["name"] + (" 🖼️" if e.get("file") else "")
        return line + (f" <@&{e['role']}>" if e.get("role") else "")

    game_lines = [game_line(e) for e in store.games(gid).values()]
    recruit = store.recruit_channel(gid)
    embed = discord.Embed(title="통화방 봇 설정", color=discord.Color.blurple())
    embed.add_field(name="생성 채널", value="\n".join(hub_lines) or "없음 (`/통화방설정 만들기`)", inline=False)
    embed.add_field(name="등록된 게임 (🖼️ = 아이콘, 옆은 자동 멘션 역할)",
                    value=", ".join(sorted(game_lines)) or "없음 (`/통화방설정 게임추가`)", inline=False)
    embed.add_field(name="구인 채널", value=f"<#{recruit}>" if recruit else "명령어 친 채널", inline=False)
    auto = store.guild(gid).get("auto_roles", [])
    embed.add_field(name="자동 역할", value=" ".join(f"<@&{r}>" for r in auto) or "없음", inline=False)
    log_ch = store.guild(gid).get("log")
    embed.add_field(name="로그 채널", value=f"<#{log_ch}>" if log_ch else "없음", inline=False)
    room_cat = store.room_category(gid)
    embed.add_field(name="방 카테고리 (기본)", value=f"📁 <#{room_cat}>" if room_cat else "생성 채널이 있는 카테고리",
                    inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@setup_group.command(name="로그채널", description="방 생성·삭제, 방장 변경, 설정 변경 같은 기록을 남길 채널 (비우면 끔)")
async def cmd_setup_log(interaction: discord.Interaction, 채널: discord.TextChannel | None = None):
    store.guild(interaction.guild.id)["log"] = 채널.id if 채널 else None
    store.save()
    msg = f"이제 {채널.mention}에 기록을 남겨요!" if 채널 else "로그 껐어요!"
    await interaction.response.send_message(msg, ephemeral=True)


@setup_group.command(name="카테고리", description="통화방이 만들어질 카테고리를 정해요")
@app_commands.describe(카테고리="방이 만들어질 카테고리 (비우면 기본값으로 되돌림)",
                       생성채널="이 생성 채널로 만든 방에만 적용 (비우면 서버 전체 기본값)")
async def cmd_setup_category(interaction: discord.Interaction, 카테고리: discord.CategoryChannel | None = None,
                             생성채널: discord.VoiceChannel | None = None):
    gid = interaction.guild.id
    cat_id = 카테고리.id if 카테고리 else None
    if 생성채널:
        if not store.set_hub_category(gid, 생성채널.id, cat_id):
            await interaction.response.send_message(f"{생성채널.mention}은(는) 생성 채널이 아니에요!", ephemeral=True)
            return
        msg = (f"{생성채널.mention}으로 만든 방은 이제 📁 **{카테고리.name}** 카테고리에 생겨요!" if 카테고리
               else f"{생성채널.mention} 전용 카테고리 해제! 이제 서버 기본 카테고리를 따라가요.")
    else:
        store.set_room_category(gid, cat_id)
        msg = (f"통화방은 이제 기본으로 📁 **{카테고리.name}** 카테고리에 생겨요!" if 카테고리
               else "기본 카테고리 해제! 이제 생성 채널이 있는 카테고리에 방이 생겨요.")
    if 카테고리:
        perms = 카테고리.permissions_for(interaction.guild.me)
        if not (perms.manage_channels and perms.move_members):
            msg += "\n⚠️ 봇한테 이 카테고리의 '채널 관리'나 '멤버 이동' 권한이 없어요! 카테고리 권한을 확인해 보세요."
    await interaction.response.send_message(msg, ephemeral=True)


@setup_group.command(name="구인채널", description="구인 글이 올라갈 채팅 채널을 정해요 (안 정하면 명령어 친 채널)")
async def cmd_setup_recruit(interaction: discord.Interaction, 채널: discord.TextChannel | None = None):
    store.set_recruit_channel(interaction.guild.id, 채널.id if 채널 else None)
    msg = f"구인 글은 이제 {채널.mention}에 올라가요!" if 채널 else "구인 채널 해제! 이제 명령어 친 채널에 올라가요."
    await interaction.response.send_message(msg, ephemeral=True)


@setup_group.command(name="게임역할", description="게임에 역할을 연결해요. 그 게임으로 구인하면 역할이 자동으로 멘션돼요")
@app_commands.describe(게임="등록된 게임", 역할="자동으로 멘션할 역할 (비우면 연결 해제)")
@app_commands.autocomplete(게임=game_autocomplete)
async def cmd_setup_game_role(interaction: discord.Interaction, 게임: str, 역할: discord.Role | None = None):
    game = await resolve_game(interaction, 게임)
    if not game:
        return
    store.set_game_role(interaction.guild.id, game, 역할.id if 역할 else None)
    if not 역할:
        await interaction.response.send_message(f"**{game}** 역할 연결 해제!", ephemeral=True)
        return
    msg = f"이제 **{game}** 구인 글에 {역할.mention}이(가) 자동으로 멘션돼요!"
    if not mention_ok(interaction.guild, [역할]):
        msg += ("\n⚠️ 지금은 봇이 이 역할을 멘션할 수 없어요! 서버 설정 → 역할 → 이 역할에서 "
                "'모든 사람이 이 역할을 @멘션하도록 허용'을 켜 주세요.")
    await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@setup_group.command(name="게임추가", description="게임을 등록해요 (아이콘은 선택, 이미 있는 게임이면 아이콘만 바꿔요)")
@app_commands.describe(게임="게임 이름 (예: 발로란트)", 아이콘="구인 이미지에 나올 아이콘 (PNG/JPG, 선택)")
@app_commands.autocomplete(게임=game_autocomplete)
async def cmd_setup_game_add(interaction: discord.Interaction, 게임: app_commands.Range[str, 1, 50],
                             아이콘: discord.Attachment | None = None):
    gid = interaction.guild.id
    old = store.game(gid, 게임)
    name = old["name"] if old else 게임.strip()
    if 아이콘 is None:
        store.set_game(gid, name, old.get("file") if old else None)
        await interaction.response.send_message(f"**{name}** 게임 등록했어요!", ephemeral=True)
        return
    if not (아이콘.content_type or "").startswith("image/") or 아이콘.size > 5 * 1024 * 1024:
        await interaction.response.send_message("아이콘은 5MB 이하 PNG/JPG만 돼요!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    data = await 아이콘.read()

    def save():
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGBA")
        side = min(im.size)  # 가운데 기준 정사각형으로 자르기
        left, top = (im.width - side) // 2, (im.height - side) // 2
        im = im.crop((left, top, left + side, top + side)).resize((256, 256), Image.LANCZOS)
        os.makedirs(ICON_DIR, exist_ok=True)
        path = os.path.join(ICON_DIR, f"{gid}_{hashlib.md5(game_key(name).encode()).hexdigest()[:12]}.png")
        im.save(path, "PNG")
        return path

    try:
        path = await asyncio.to_thread(save)
    except Exception:
        await interaction.followup.send("이미지를 못 읽었어요! 다른 파일로 해 주세요.", ephemeral=True)
        return
    store.set_game(gid, name, path)
    await interaction.followup.send(f"**{name}** 게임을 아이콘이랑 같이 등록했어요!", ephemeral=True)


async def source_guild_autocomplete(interaction: discord.Interaction, current: str):
    out = []
    for g in bot.guilds:
        if g.id == interaction.guild_id or not store.games(g.id) or current.lower() not in g.name.lower():
            continue
        member = g.get_member(interaction.user.id)
        if member is not None and not member.guild_permissions.manage_guild:
            continue
        out.append(app_commands.Choice(name=f"{g.name} (게임 {len(store.games(g.id))}개)"[:100], value=str(g.id)))
    return out[:25]


@setup_group.command(name="게임가져오기", description="다른 서버에 등록한 게임과 아이콘을 이 서버로 복사해요")
@app_commands.describe(서버="가져올 서버 (두 서버 모두 서버 관리 권한이 있어야 해요)",
                       덮어쓰기="이 서버에 이미 있는 게임도 아이콘을 바꿀지 (기본: 건너뜀)")
@app_commands.autocomplete(서버=source_guild_autocomplete)
async def cmd_setup_game_import(interaction: discord.Interaction, 서버: str, 덮어쓰기: bool = False):
    source = bot.get_guild(int(서버)) if 서버.isdigit() else None
    if source is None or source.id == interaction.guild.id:
        await interaction.response.send_message("목록에서 서버를 골라 주세요!", ephemeral=True)
        return
    try:
        member = source.get_member(interaction.user.id) or await source.fetch_member(interaction.user.id)
    except discord.HTTPException:
        member = None
    if member is None or not member.guild_permissions.manage_guild:
        await interaction.response.send_message(f"**{source.name}**에서도 서버 관리 권한이 있어야 가져올 수 있어요!",
                                                ephemeral=True)
        return
    gid = interaction.guild.id
    added, updated, skipped = [], [], []
    for entry in store.games(source.id).values():
        name = entry["name"]
        existing = store.game(gid, name)
        if existing and not 덮어쓰기:
            skipped.append(name)
            continue
        icon = existing.get("file") if existing else None
        if entry.get("file") and os.path.exists(entry["file"]):
            os.makedirs(ICON_DIR, exist_ok=True)
            icon = os.path.join(ICON_DIR, f"{gid}_{hashlib.md5(game_key(name).encode()).hexdigest()[:12]}.png")
            shutil.copyfile(entry["file"], icon)
        store.set_game(gid, existing["name"] if existing else name, icon)
        (updated if existing else added).append(name)
    lines = []
    if added:
        lines.append(f"추가 {len(added)}개: {', '.join(added)}")
    if updated:
        lines.append(f"덮어씀 {len(updated)}개: {', '.join(updated)}")
    if skipped:
        lines.append(f"이미 있어서 건너뜀 {len(skipped)}개: {', '.join(skipped)}")
    await interaction.response.send_message("\n".join(lines) or "가져올 게임이 없어요!", ephemeral=True)


@setup_group.command(name="게임삭제", description="등록한 게임을 지워요")
@app_commands.autocomplete(게임=game_autocomplete)
async def cmd_setup_game_remove(interaction: discord.Interaction, 게임: str):
    entry = store.game(interaction.guild.id, 게임)
    if not entry:
        await interaction.response.send_message("등록된 게임이 아니에요!", ephemeral=True)
        return
    if entry.get("file") and os.path.exists(entry["file"]):
        os.remove(entry["file"])
    store.remove_game(interaction.guild.id, 게임)
    await interaction.response.send_message(
        f"**{entry['name']}** 게임 지웠어요! 이 게임 생성 채널이 있으면 `/통화방설정 해제`로 정리해 주세요.",
        ephemeral=True)
bot.tree.add_command(setup_group)
