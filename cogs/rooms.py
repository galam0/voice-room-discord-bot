"""통화방 생성·삭제·방장, 컨트롤 패널, /통화방"""

import asyncio

from discord import app_commands
import discord

from core.config import EMPTY_GRACE_SECONDS, log
from core.store import store
from core.client import bot
from core.utils import admin_log, default_name, existing_room, humans, notify, owner_tag, try_rename, user_lock
from core.games import game_autocomplete, resolve_game
from cogs.recruit import close_post


async def delete_room(channel: discord.VoiceChannel, reason: str):
    asyncio.create_task(admin_log(channel.guild, f"⚫ 방 삭제 · **{channel.name}** ({reason})"))
    store.remove_room(channel.id)
    await close_post(channel.id)
    try:
        await channel.delete(reason=reason)
    except discord.NotFound:
        pass
    except discord.HTTPException as e:
        log.warning("채널 삭제 실패 %s: %s", channel.id, e)


def first_hub_category(guild: discord.Guild):
    for cid in store.hubs(guild.id):
        ch = guild.get_channel(int(cid))
        if ch and ch.category:
            return ch.category
    return None


def configured_category(guild: discord.Guild, category_id):
    ch = guild.get_channel(category_id) if category_id else None
    return ch if isinstance(ch, discord.CategoryChannel) else None


def hub_room_category(guild: discord.Guild, hub_channel, hub_cfg: dict):
    """생성 채널로 만든 방의 카테고리: 그 생성 채널 전용 > 서버 기본 > 생성 채널이 있는 카테고리."""
    return (configured_category(guild, hub_cfg.get("category"))
            or configured_category(guild, store.room_category(guild.id))
            or hub_channel.category)


def pick_category(guild: discord.Guild, member: discord.Member, fallback):
    """명령어로 만든 방의 카테고리: 서버 기본 > 생성 채널 카테고리 > 지금 있는 음성 채널 > 명령어 친 채널."""
    category = configured_category(guild, store.room_category(guild.id)) or first_hub_category(guild)
    if category:
        return category
    if member.voice and member.voice.channel and member.voice.channel.category:
        return member.voice.channel.category
    return getattr(fallback, "category", None)


async def create_room(member: discord.Member, category, name=None, limit=0, game=None) -> discord.VoiceChannel:
    overwrites = dict(category.overwrites) if category else {}
    channel = await member.guild.create_voice_channel(
        name=(name or default_name(member, game))[:100],
        category=category,
        user_limit=limit,
        overwrites=overwrites,
        reason=f"{member} 통화방 생성",
    )
    store.add_room(channel.id, member.id, member.guild.id, custom_name=bool(name), game=game)
    log.info("방 생성: %s (방장 %s)", channel.name, member)
    asyncio.create_task(admin_log(member.guild, f"🟢 방 생성 · {channel.mention} · {member.mention}"))
    asyncio.create_task(send_panel(channel, member))
    return channel


async def transfer_owner(channel: discord.VoiceChannel, new_owner: discord.Member):
    room = store.room(channel.id)
    old = f"<@{room['owner']}>" if room else "?"
    store.update_room(channel.id, owner=new_owner.id)
    asyncio.create_task(admin_log(channel.guild, f"👑 방장 변경 · {channel.mention} · {old} → {new_owner.mention}"))
    if room and room["locked"]:
        await channel.set_permissions(new_owner, connect=True, reason="방장 위임")
    await notify(channel, f"👑 {new_owner.mention}님이 방장이 됐어요!")
    if room and not room["custom_name"]:
        # 이름 변경은 제한이 있어서 기다리지 않게 백그라운드로
        asyncio.create_task(try_rename(channel, default_name(new_owner, room.get("game"))))


async def schedule_empty_check(channel_id: int, delay: float | None = None):
    """명령어로 만들고 아무도 안 들어온 방 정리."""
    await asyncio.sleep(EMPTY_GRACE_SECONDS if delay is None else delay)
    ch = bot.get_channel(channel_id)
    if ch and store.room(channel_id) and not humans(ch):
        await delete_room(ch, "아무도 들어오지 않은 통화방")


# ─────────────────────────────── 방장 기능 (명령어·버튼 공용) ───────────────────────────────
async def lock_room(ch: discord.VoiceChannel, owner: discord.Member):
    guild = ch.guild
    overwrites = dict(ch.overwrites)
    # 모든 역할(@everyone 포함)은 입장 금지
    for target in [guild.default_role] + [t for t in overwrites if isinstance(t, discord.Role)]:
        ow = overwrites.get(target, discord.PermissionOverwrite())
        ow.connect = False
        overwrites[target] = ow
    # 지금 있는 사람, 방장, 봇은 허용
    for m in set(ch.members) | {owner, guild.me}:
        ow = overwrites.get(m, discord.PermissionOverwrite())
        ow.connect = True
        overwrites[m] = ow
    await ch.edit(overwrites=overwrites)
    store.update_room(ch.id, locked=True)
    await admin_log(guild, f"🔒 잠금 · {ch.mention} · {owner.mention}")


async def unlock_room(ch: discord.VoiceChannel):
    # 카테고리 기본 권한으로 되돌림
    await ch.edit(overwrites=dict(ch.category.overwrites) if ch.category else {})
    store.update_room(ch.id, locked=False)
    await admin_log(ch.guild, f"🔓 공개 · {ch.mention} · {owner_tag(ch)}")


async def allow_member(ch: discord.VoiceChannel, member: discord.Member, by) -> str:
    await ch.set_permissions(member, connect=True, reason=f"{by} 허용")
    await admin_log(ch.guild, f"✅ 허용 · {ch.mention} · {member.mention}")
    extra = "" if store.room(ch.id)["locked"] else " (지금은 공개라 원래 누구나 들어올 수 있어요)"
    return f"이제 {member.mention}님도 {ch.mention}에 들어올 수 있어요!{extra}"


async def rename_room(ch: discord.VoiceChannel, name: str) -> str:
    if await try_rename(ch, name):
        store.update_room(ch.id, custom_name=True)
        await admin_log(ch.guild, f"✏️ 이름 변경 · {ch.mention} · {owner_tag(ch)}")
        return f"이름을 **{name}**(으)로 바꿨어요!"
    return "이름은 10분에 2번까지만 바꿀 수 있어요! 조금 있다가 다시 해 주세요. (디스코드 제한)"


async def set_limit(ch: discord.VoiceChannel, limit: int) -> str:
    await ch.edit(user_limit=limit)
    await admin_log(ch.guild, f"👥 인원 {limit or '제한 없음'} · {ch.mention} · {owner_tag(ch)}")
    return "인원 제한 없앴어요!" if limit == 0 else f"인원 **{limit}명**으로 바꿨어요!"


def transfer_problem(ch: discord.VoiceChannel, actor: discord.Member, target: discord.Member):
    """방장 넘기기가 안 되는 이유 (되면 None)."""
    if target.bot or target.id == actor.id:
        return "다른 사람에게만 넘길 수 있어요!"
    if target not in ch.members:
        return f"{target.mention}님이 {ch.mention}에 있어야 넘길 수 있어요!"
    if store.room_of_owner(ch.guild.id, target.id) is not None:
        return "그 사람은 이미 다른 방 방장이에요!"
    return None


# ─────────────────────────────── 컨트롤 패널 ───────────────────────────────
def panel_view(channel_id: int) -> discord.ui.View:
    """버튼은 custom_id로 on_interaction에서 처리 → 봇이 재시작돼도 동작."""
    view = discord.ui.View(timeout=None)
    buttons = [
        ("lock", "잠금", "🔒", discord.ButtonStyle.secondary, 0),
        ("unlock", "공개", "🔓", discord.ButtonStyle.secondary, 0),
        ("allow", "허용", "✅", discord.ButtonStyle.secondary, 0),
        ("rename", "이름", "✏️", discord.ButtonStyle.primary, 1),
        ("limit", "인원", "👥", discord.ButtonStyle.primary, 1),
        ("transfer", "방장넘기기", "👑", discord.ButtonStyle.primary, 1),
    ]
    for action, label, emoji, style, row in buttons:
        view.add_item(discord.ui.Button(label=label, emoji=emoji, style=style, row=row,
                                        custom_id=f"panel:{action}:{channel_id}"))
    return view


async def send_panel(channel: discord.VoiceChannel, owner: discord.Member):
    embed = discord.Embed(
        title="통화방 설정",
        description=f"방장 {owner.mention}",
        color=discord.Color.blurple(),
    )
    try:
        await channel.send(embed=embed, view=panel_view(channel.id))
    except discord.HTTPException as e:
        log.warning("패널 전송 실패 %s: %s", channel.id, e)


class RenameModal(discord.ui.Modal, title="통화방 이름 바꾸기"):
    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel
        self.name = discord.ui.TextInput(label="새 이름", max_length=100, default=channel.name)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_owner(self.channel, interaction.user):
            await interaction.response.send_message("방장만 바꿀 수 있어요!", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await rename_room(self.channel, self.name.value.strip()), ephemeral=True)


class LimitModal(discord.ui.Modal, title="인원 제한 바꾸기"):
    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel
        self.limit = discord.ui.TextInput(label="인원 (0~99, 0 = 제한 없음)", max_length=2,
                                          default=str(channel.user_limit))
        self.add_item(self.limit)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_owner(self.channel, interaction.user):
            await interaction.response.send_message("방장만 바꿀 수 있어요!", ephemeral=True)
            return
        value = self.limit.value.strip()
        if not value.isdigit() or not 0 <= int(value) <= 99:
            await interaction.response.send_message("0~99 사이 숫자로 적어 주세요!", ephemeral=True)
            return
        await interaction.response.send_message(await set_limit(self.channel, int(value)), ephemeral=True)


class PickMemberView(discord.ui.View):
    """허용 / 방장넘기기에서 사람 고르는 선택 메뉴 (본인에게만 보임)."""

    def __init__(self, channel: discord.VoiceChannel, mode: str):
        super().__init__(timeout=120)
        self.channel, self.mode = channel, mode
        placeholder = "들어올 수 있게 할 사람" if mode == "allow" else "방장을 넘길 사람 (같은 방에 있어야 해요)"
        self.select = discord.ui.UserSelect(placeholder=placeholder)
        self.select.callback = self.picked
        self.add_item(self.select)

    async def picked(self, interaction: discord.Interaction):
        if not is_owner(self.channel, interaction.user):
            await interaction.response.edit_message(content="이제 방장이 아니에요!", view=None)
            return
        target = self.select.values[0]
        if not isinstance(target, discord.Member):
            target = interaction.guild.get_member(target.id)
        if target is None:
            await interaction.response.edit_message(content="그 사람을 못 찾았어요!", view=None)
            return
        if self.mode == "allow":
            await interaction.response.edit_message(
                content=await allow_member(self.channel, target, interaction.user), view=None)
        else:
            problem = transfer_problem(self.channel, interaction.user, target)
            if problem:
                await interaction.response.edit_message(content=problem, view=None)
                return
            await interaction.response.edit_message(content=f"{target.mention}님에게 방장을 넘겼어요!", view=None)
            await transfer_owner(self.channel, target)


def is_owner(channel: discord.VoiceChannel, user) -> bool:
    room = store.room(channel.id)
    return bool(room) and room["owner"] == user.id


async def handle_panel(interaction: discord.Interaction, action: str, channel_id: int):
    ch = interaction.guild.get_channel(channel_id)
    if ch is None or not store.room(channel_id):
        await interaction.response.send_message("이미 사라진 통화방이에요!", ephemeral=True)
        return
    if not is_owner(ch, interaction.user):
        await interaction.response.send_message(f"방장(<@{store.room(channel_id)['owner']}>)만 누를 수 있어요!",
                                                ephemeral=True)
        return
    if action == "lock":
        await interaction.response.defer(ephemeral=True)
        await lock_room(ch, interaction.user)
        await interaction.followup.send("🔒 잠갔어요!", ephemeral=True)
    elif action == "unlock":
        await interaction.response.defer(ephemeral=True)
        await unlock_room(ch)
        await interaction.followup.send("🔓 공개로 바꿨어요!", ephemeral=True)
    elif action == "rename":
        await interaction.response.send_modal(RenameModal(ch))
    elif action == "limit":
        await interaction.response.send_modal(LimitModal(ch))
    elif action in ("allow", "transfer"):
        label = "누구를 들어오게 할까요?" if action == "allow" else "누구에게 방장을 넘길까요?"
        await interaction.response.send_message(label, view=PickMemberView(ch, action), ephemeral=True)


# ─────────────────────────────── /통화방 ───────────────────────────────
room_group = app_commands.Group(name="통화방", description="통화방 만들기와 관리", guild_only=True)


async def owned_room(interaction: discord.Interaction):
    """명령어 친 사람이 방장인 방. 없으면 안내하고 None."""
    ch = existing_room(interaction.guild, interaction.user)
    if ch is None:
        await interaction.response.send_message("내 통화방이 없어요! `/통화방 생성`으로 먼저 만들어 주세요.", ephemeral=True)
    return ch


@room_group.command(name="생성", description="내 통화방을 만들어요")
@app_commands.describe(이름="방 이름 (비우면 'OOO의 통화방')", 인원="인원 제한 (0 = 제한 없음)",
                       게임="등록된 게임 중에서 고르기 (선택)")
@app_commands.autocomplete(게임=game_autocomplete)
async def cmd_create(interaction: discord.Interaction, 이름: str | None = None,
                     인원: app_commands.Range[int, 0, 99] = 0, 게임: str | None = None):
    member = interaction.user
    game = await resolve_game(interaction, 게임)
    if game is False:
        return
    async with user_lock(member.id):
        ch = existing_room(interaction.guild, member)
        if ch:
            await interaction.response.send_message(f"이미 내 방이 있어요! {ch.mention}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        category = pick_category(interaction.guild, member, interaction.channel)
        try:
            ch = await create_room(member, category, name=이름, limit=인원, game=game)
        except discord.HTTPException:
            await interaction.followup.send("방을 못 만들었어요! 봇 권한(채널 관리)을 확인해 보세요.", ephemeral=True)
            return

    if member.voice and member.voice.channel:
        try:
            await member.move_to(ch)
            await interaction.followup.send(f"{ch.mention} 만들고 옮겨 드렸어요!", ephemeral=True)
            return
        except discord.HTTPException:
            pass
    await interaction.followup.send(
        f"{ch.mention} 만들었어요! {EMPTY_GRACE_SECONDS}초 안에 아무도 안 들어오면 사라져요.", ephemeral=True)
    asyncio.create_task(schedule_empty_check(ch.id))


@room_group.command(name="이름", description="내 통화방 이름을 바꿔요")
async def cmd_rename(interaction: discord.Interaction, 새이름: app_commands.Range[str, 1, 100]):
    ch = await owned_room(interaction)
    if not ch:
        return
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(await rename_room(ch, 새이름), ephemeral=True)


@room_group.command(name="인원", description="내 통화방 인원 제한을 바꿔요 (0 = 제한 없음)")
async def cmd_limit(interaction: discord.Interaction, 인원: app_commands.Range[int, 0, 99]):
    ch = await owned_room(interaction)
    if not ch:
        return
    await interaction.response.send_message(await set_limit(ch, 인원), ephemeral=True)


@room_group.command(name="잠금", description="지금 있는 사람 말고는 못 들어오게 잠가요")
async def cmd_lock(interaction: discord.Interaction):
    ch = await owned_room(interaction)
    if not ch:
        return
    await interaction.response.defer(ephemeral=True)
    await lock_room(ch, interaction.user)
    await interaction.followup.send("🔒 잠갔어요!", ephemeral=True)


@room_group.command(name="공개", description="잠금을 풀어서 누구나 들어올 수 있게 해요")
async def cmd_unlock(interaction: discord.Interaction):
    ch = await owned_room(interaction)
    if not ch:
        return
    await interaction.response.defer(ephemeral=True)
    await unlock_room(ch)
    await interaction.followup.send("🔓 공개로 바꿨어요!", ephemeral=True)


@room_group.command(name="허용", description="잠긴 내 통화방에 특정 사람이 들어올 수 있게 해요")
async def cmd_allow(interaction: discord.Interaction, 멤버: discord.Member):
    ch = await owned_room(interaction)
    if not ch:
        return
    await interaction.response.send_message(await allow_member(ch, 멤버, interaction.user), ephemeral=True)


@room_group.command(name="방장넘기기", description="같은 방에 있는 사람에게 방장을 넘겨요")
async def cmd_transfer(interaction: discord.Interaction, 멤버: discord.Member):
    ch = await owned_room(interaction)
    if not ch:
        return
    problem = transfer_problem(ch, interaction.user, 멤버)
    if problem:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    await interaction.response.send_message(f"{멤버.mention}님에게 방장을 넘겼어요!", ephemeral=True)
    await transfer_owner(ch, 멤버)


@room_group.command(name="패널", description="내 통화방 컨트롤 패널을 다시 올려요")
async def cmd_panel(interaction: discord.Interaction):
    ch = await owned_room(interaction)
    if not ch:
        return
    await send_panel(ch, interaction.user)
    await interaction.response.send_message(f"{ch.mention} 채팅에 패널 올렸어요!", ephemeral=True)


@room_group.command(name="정보", description="지금 있는 통화방(또는 내 방) 정보를 보여줘요")
async def cmd_info(interaction: discord.Interaction):
    member = interaction.user
    ch = None
    if member.voice and member.voice.channel and store.room(member.voice.channel.id):
        ch = member.voice.channel
    ch = ch or existing_room(interaction.guild, member)
    if not ch:
        await interaction.response.send_message("통화방에 들어가 있어야 볼 수 있어요!", ephemeral=True)
        return
    room = store.room(ch.id)
    embed = discord.Embed(title=ch.name, color=discord.Color.blurple())
    embed.add_field(name="방장", value=f"<@{room['owner']}>")
    embed.add_field(name="인원", value=f"{len(ch.members)} / {ch.user_limit or '제한 없음'}")
    embed.add_field(name="상태", value="🔒 잠금" if room["locked"] else "🔓 공개")
    if room.get("game"):
        embed.add_field(name="게임", value=room["game"])
    await interaction.response.send_message(embed=embed, ephemeral=True)


bot.tree.add_command(room_group)
