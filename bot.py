"""
통화방 자동 생성 디스코드 봇 (Join to Create)

- "➕ 통화방 만들기" 채널에 들어가면 "OOO의 통화방"을 만들어 자동 이동
- /통화방 생성 명령어로도 생성 가능
- 빈 방은 자동 삭제
- 방장 전용: 이름 변경, 인원 제한, 잠금/공개, 잠긴 방에 허용, 방장 넘기기
- 방장이 나가면 남은 사람에게 방장 자동 위임
- 한 사람당 방 1개
- /구인: 지금 있는 통화방으로 사람 구하는 글 (입장 버튼, 인원 자동 갱신, 방 비면 자동 마감)
"""

import asyncio
import json
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import recruit_card

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATA_FILE = os.getenv("DATA_FILE", "data.json")
# 명령어로 만든 방에 아무도 안 들어오면 이 시간(초) 뒤 삭제
EMPTY_GRACE_SECONDS = int(os.getenv("EMPTY_GRACE_SECONDS", "60"))
# 디스코드는 채널 이름 변경을 10분에 2번으로 제한함 → 오래 기다리지 않고 포기할 시간
RENAME_TIMEOUT = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("voicebot")


# ─────────────────────────────── 저장소 ───────────────────────────────
class Store:
    """서버별 생성 채널(hub)과 만들어진 방 목록을 JSON 파일에 저장."""

    def __init__(self, path: str):
        self.path = path
        self.data = {"guilds": {}, "rooms": {}}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        self.data.setdefault("guilds", {})
        self.data.setdefault("rooms", {})
        self.data.setdefault("posts", {})

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # 생성 채널(hub)
    def hub(self, guild_id: int):
        return self.data["guilds"].get(str(guild_id), {}).get("hub")

    def set_hub(self, guild_id: int, channel_id):
        self.data["guilds"].setdefault(str(guild_id), {})["hub"] = channel_id
        self.save()

    # 방
    def room(self, channel_id: int):
        return self.data["rooms"].get(str(channel_id))

    def add_room(self, channel_id: int, owner_id: int, guild_id: int, custom_name: bool):
        self.data["rooms"][str(channel_id)] = {
            "owner": owner_id,
            "guild": guild_id,
            "custom_name": custom_name,  # 방장이 직접 이름을 정했으면 방장 바뀌어도 이름 유지
            "locked": False,
        }
        self.save()

    def update_room(self, channel_id: int, **fields):
        self.data["rooms"][str(channel_id)].update(fields)
        self.save()

    def remove_room(self, channel_id: int):
        if self.data["rooms"].pop(str(channel_id), None) is not None:
            self.save()

    def room_of_owner(self, guild_id: int, user_id: int):
        for cid, r in self.data["rooms"].items():
            if r["guild"] == guild_id and r["owner"] == user_id:
                return int(cid)
        return None

    # 구인 채널
    def recruit_channel(self, guild_id: int):
        return self.data["guilds"].get(str(guild_id), {}).get("recruit")

    def set_recruit_channel(self, guild_id: int, channel_id):
        self.data["guilds"].setdefault(str(guild_id), {})["recruit"] = channel_id
        self.save()

    # 구인 글 (음성 채널 하나당 글 하나)
    def post(self, voice_id: int):
        return self.data["posts"].get(str(voice_id))

    def set_post(self, voice_id: int, **info):
        self.data["posts"][str(voice_id)] = info
        self.save()

    def remove_post(self, voice_id: int):
        if self.data["posts"].pop(str(voice_id), None) is not None:
            self.save()


store = Store(DATA_FILE)

intents = discord.Intents.default()
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 같은 사람이 동시에 방을 두 개 만드는 걸 막는 잠금
_user_locks: dict[int, asyncio.Lock] = {}


def user_lock(user_id: int) -> asyncio.Lock:
    return _user_locks.setdefault(user_id, asyncio.Lock())


# ─────────────────────────────── 도우미 ───────────────────────────────
def default_name(member: discord.Member) -> str:
    return f"{member.display_name}의 통화방"[:100]


def humans(channel: discord.VoiceChannel):
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


async def delete_room(channel: discord.VoiceChannel, reason: str):
    store.remove_room(channel.id)
    await close_post(channel.id)
    try:
        await channel.delete(reason=reason)
    except discord.NotFound:
        pass
    except discord.HTTPException as e:
        log.warning("채널 삭제 실패 %s: %s", channel.id, e)


def pick_category(guild: discord.Guild, member: discord.Member, fallback):
    """새 방을 만들 카테고리: 생성 채널 카테고리 > 지금 있는 음성 채널 카테고리 > 명령어 친 채널 카테고리."""
    hub_id = store.hub(guild.id)
    hub = guild.get_channel(hub_id) if hub_id else None
    if hub and hub.category:
        return hub.category
    if member.voice and member.voice.channel and member.voice.channel.category:
        return member.voice.channel.category
    return getattr(fallback, "category", None)


async def create_room(member: discord.Member, category, name=None, limit=0) -> discord.VoiceChannel:
    overwrites = dict(category.overwrites) if category else {}
    channel = await member.guild.create_voice_channel(
        name=(name or default_name(member))[:100],
        category=category,
        user_limit=limit,
        overwrites=overwrites,
        reason=f"{member} 통화방 생성",
    )
    store.add_room(channel.id, member.id, member.guild.id, custom_name=bool(name))
    log.info("방 생성: %s (방장 %s)", channel.name, member)
    return channel


def existing_room(guild: discord.Guild, member: discord.Member):
    """이미 가진 방이 있으면 반환 (채널이 사라졌으면 기록 정리)."""
    cid = store.room_of_owner(guild.id, member.id)
    if cid is None:
        return None
    ch = guild.get_channel(cid)
    if ch is None:
        store.remove_room(cid)
    return ch


async def transfer_owner(channel: discord.VoiceChannel, new_owner: discord.Member):
    room = store.room(channel.id)
    store.update_room(channel.id, owner=new_owner.id)
    if room and room["locked"]:
        await channel.set_permissions(new_owner, connect=True, reason="방장 위임")
    await notify(channel, f"👑 {new_owner.mention}님이 새 방장이 됐어요.")
    if room and not room["custom_name"]:
        # 이름 변경은 제한이 있어서 기다리지 않게 백그라운드로
        asyncio.create_task(try_rename(channel, default_name(new_owner)))


async def schedule_empty_check(channel_id: int):
    """명령어로 만들고 아무도 안 들어온 방 정리."""
    await asyncio.sleep(EMPTY_GRACE_SECONDS)
    ch = bot.get_channel(channel_id)
    if ch and store.room(channel_id) and not humans(ch):
        await delete_room(ch, "아무도 들어오지 않은 통화방")


# ─────────────────────────────── 구인구직 ───────────────────────────────
async def _avatar_bytes(member: discord.Member):
    try:
        asset = member.display_avatar.replace(size=128, static_format="png")
        return await asyncio.wait_for(asset.read(), timeout=5)
    except Exception:
        return None


async def recruit_card_file(info: dict, voice, closed: bool = False) -> discord.File:
    """구인 글 이미지를 만들어 discord.File로 돌려줌."""
    members = humans(voice) if voice else []
    members.sort(key=lambda m: m.id != info["author"])  # 구인자가 맨 앞
    avatars = await asyncio.gather(*[_avatar_bytes(m) for m in members[:recruit_card.MAX_SLOTS]])
    if closed:
        status = "closed"
    elif len(members) >= info["target"]:
        status = "full"
    else:
        status = "open"
    buf = await asyncio.to_thread(
        recruit_card.render_card,
        title=info.get("game") or "구인",
        content=info["content"],
        author=info["author_name"],
        voice_name=voice.name if voice else info["voice_name"],
        target=info["target"],
        members=[(m.display_name, a) for m, a in zip(members, avatars)],
        status=status,
    )
    return discord.File(buf, filename="recruit.png")


def recruit_view(info: dict, jump_url: str | None) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if jump_url:
        view.add_item(discord.ui.Button(label="🔊 통화방 들어가기", style=discord.ButtonStyle.link, url=jump_url))
    view.add_item(discord.ui.Button(label="마감", style=discord.ButtonStyle.danger,
                                    custom_id=f"recruit_close:{info['voice']}"))
    return view


async def fetch_post_message(info: dict):
    ch = bot.get_channel(info["channel"])
    if ch is None:
        return None
    try:
        return await ch.fetch_message(info["message"])
    except discord.HTTPException:
        return None


async def refresh_post(voice_id: int):
    """통화방 인원이 바뀌면 구인 글 인원 갱신, 방이 비면 자동 마감."""
    info = store.post(voice_id)
    if not info:
        return
    voice = bot.get_channel(voice_id)
    if voice is None or not humans(voice):
        await close_post(voice_id)
        return
    msg = await fetch_post_message(info)
    if msg is None:
        store.remove_post(voice_id)
        return
    try:
        await msg.edit(attachments=[await recruit_card_file(info, voice)])
    except discord.HTTPException:
        pass


async def close_post(voice_id: int):
    info = store.post(voice_id)
    if not info:
        return
    store.remove_post(voice_id)
    msg = await fetch_post_message(info)
    if msg is None:
        return
    voice = bot.get_channel(voice_id)
    try:
        await msg.edit(attachments=[await recruit_card_file(info, voice, closed=True)], view=None)
    except discord.HTTPException:
        pass


# ─────────────────────────────── 이벤트 ───────────────────────────────
@bot.event
async def on_ready():
    # 꺼져 있는 동안 비거나 사라진 방 정리
    for cid in list(store.data["rooms"].keys()):
        ch = bot.get_channel(int(cid))
        if ch is None:
            store.remove_room(int(cid))
        elif not humans(ch):
            await delete_room(ch, "봇 재시작 후 빈 통화방 정리")
    for vid in list(store.data["posts"].keys()):
        await refresh_post(int(vid))
    synced = await bot.tree.sync()
    log.info("로그인: %s / 명령어 %d개 동기화", bot.user, len(synced))


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot or before.channel == after.channel:
        return
    guild = member.guild

    # 1) 생성 채널에 들어옴 → 방 만들고 이동
    if after.channel and after.channel.id == store.hub(guild.id):
        async with user_lock(member.id):
            room = existing_room(guild, member)
            created = room is None
            if created:
                try:
                    room = await create_room(member, after.channel.category)
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
            await delete_room(channel, "빈 통화방")
        elif room["owner"] == member.id:
            # 이미 다른 방을 가진 사람은 피해서 위임
            candidates = [m for m in left if store.room_of_owner(guild.id, m.id) is None] or left
            await transfer_owner(channel, candidates[0])

    # 3) 구인 글이 걸린 채널이면 인원 갱신 (봇이 안 만든 일반 음성 채널도 포함)
    for ch in {before.channel, after.channel}:
        if ch and store.post(ch.id):
            await refresh_post(ch.id)


@bot.event
async def on_interaction(interaction: discord.Interaction):
    """구인 글 '마감' 버튼 (봇이 재시작돼도 동작하도록 custom_id로 처리)."""
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = (interaction.data or {}).get("custom_id", "")
    if not custom_id.startswith("recruit_close:"):
        return
    voice_id = int(custom_id.split(":", 1)[1])
    info = store.post(voice_id)
    if not info:
        await interaction.response.send_message("이미 마감된 구인 글이에요.", ephemeral=True)
        return
    is_admin = interaction.user.guild_permissions.manage_messages
    if interaction.user.id != info["author"] and not is_admin:
        await interaction.response.send_message("구인 글을 올린 사람만 마감할 수 있어요.", ephemeral=True)
        return
    await interaction.response.send_message("구인을 마감했어요.", ephemeral=True)
    await close_post(voice_id)


@bot.event
async def on_guild_channel_delete(channel):
    store.remove_room(channel.id)
    await close_post(channel.id)
    if store.recruit_channel(channel.guild.id) == channel.id:
        store.set_recruit_channel(channel.guild.id, None)
    if store.hub(channel.guild.id) == channel.id:
        store.set_hub(channel.guild.id, None)


# ─────────────────────────────── /통화방 ───────────────────────────────
room_group = app_commands.Group(name="통화방", description="통화방 만들기와 관리", guild_only=True)


async def owned_room(interaction: discord.Interaction):
    """명령어 친 사람이 방장인 방. 없으면 안내하고 None."""
    ch = existing_room(interaction.guild, interaction.user)
    if ch is None:
        await interaction.response.send_message("방장인 통화방이 없어요. `/통화방 생성`으로 먼저 만들어 주세요.", ephemeral=True)
    return ch


@room_group.command(name="생성", description="내 통화방을 만들어요")
@app_commands.describe(이름="방 이름 (비우면 'OOO의 통화방')", 인원="인원 제한 (0 = 제한 없음)")
async def cmd_create(interaction: discord.Interaction, 이름: str | None = None,
                     인원: app_commands.Range[int, 0, 99] = 0):
    member = interaction.user
    async with user_lock(member.id):
        ch = existing_room(interaction.guild, member)
        if ch:
            await interaction.response.send_message(f"이미 방이 있어요: {ch.mention}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        category = pick_category(interaction.guild, member, interaction.channel)
        try:
            ch = await create_room(member, category, name=이름, limit=인원)
        except discord.HTTPException:
            await interaction.followup.send("방을 만들지 못했어요. 봇 권한(채널 관리)을 확인해 주세요.", ephemeral=True)
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
    if await try_rename(ch, 새이름):
        store.update_room(ch.id, custom_name=True)
        await interaction.followup.send(f"이름을 **{새이름}**(으)로 바꿨어요.", ephemeral=True)
    else:
        await interaction.followup.send("디스코드 제한으로 채널 이름은 10분에 2번까지만 바꿀 수 있어요. 잠시 뒤에 다시 해 주세요.", ephemeral=True)


@room_group.command(name="인원", description="내 통화방 인원 제한을 바꿔요 (0 = 제한 없음)")
async def cmd_limit(interaction: discord.Interaction, 인원: app_commands.Range[int, 0, 99]):
    ch = await owned_room(interaction)
    if not ch:
        return
    await ch.edit(user_limit=인원)
    await interaction.response.send_message("인원 제한을 없앴어요." if 인원 == 0 else f"인원을 **{인원}명**으로 제한했어요.", ephemeral=True)


@room_group.command(name="잠금", description="지금 있는 사람 말고는 못 들어오게 잠가요")
async def cmd_lock(interaction: discord.Interaction):
    ch = await owned_room(interaction)
    if not ch:
        return
    guild = interaction.guild
    overwrites = dict(ch.overwrites)
    # 모든 역할(@everyone 포함)은 입장 금지
    for target in [guild.default_role] + [t for t in overwrites if isinstance(t, discord.Role)]:
        ow = overwrites.get(target, discord.PermissionOverwrite())
        ow.connect = False
        overwrites[target] = ow
    # 지금 있는 사람, 방장, 봇은 허용
    for m in set(ch.members) | {interaction.user, guild.me}:
        ow = overwrites.get(m, discord.PermissionOverwrite())
        ow.connect = True
        overwrites[m] = ow
    await ch.edit(overwrites=overwrites)
    store.update_room(ch.id, locked=True)
    await interaction.response.send_message("🔒 잠갔어요. 친구를 들이려면 `/통화방 허용`을 써 주세요.", ephemeral=True)


@room_group.command(name="공개", description="잠금을 풀어서 누구나 들어올 수 있게 해요")
async def cmd_unlock(interaction: discord.Interaction):
    ch = await owned_room(interaction)
    if not ch:
        return
    # 카테고리 기본 권한으로 되돌림
    await ch.edit(overwrites=dict(ch.category.overwrites) if ch.category else {})
    store.update_room(ch.id, locked=False)
    await interaction.response.send_message("🔓 공개로 바꿨어요.", ephemeral=True)


@room_group.command(name="허용", description="잠긴 내 통화방에 특정 사람이 들어올 수 있게 해요")
async def cmd_allow(interaction: discord.Interaction, 멤버: discord.Member):
    ch = await owned_room(interaction)
    if not ch:
        return
    await ch.set_permissions(멤버, connect=True, reason=f"{interaction.user} 허용")
    extra = "" if store.room(ch.id)["locked"] else " (지금은 공개 상태라 원래 누구나 들어올 수 있어요)"
    await interaction.response.send_message(f"{멤버.mention}님이 {ch.mention}에 들어올 수 있어요.{extra}", ephemeral=True)


@room_group.command(name="방장넘기기", description="같은 방에 있는 사람에게 방장을 넘겨요")
async def cmd_transfer(interaction: discord.Interaction, 멤버: discord.Member):
    ch = await owned_room(interaction)
    if not ch:
        return
    if 멤버.bot or 멤버 == interaction.user:
        await interaction.response.send_message("다른 사람(봇 제외)에게만 넘길 수 있어요.", ephemeral=True)
        return
    if 멤버 not in ch.members:
        await interaction.response.send_message(f"{멤버.mention}님이 {ch.mention}에 있어야 넘길 수 있어요.", ephemeral=True)
        return
    if store.room_of_owner(interaction.guild.id, 멤버.id) is not None:
        await interaction.response.send_message("그 사람은 이미 다른 방의 방장이에요.", ephemeral=True)
        return
    await interaction.response.send_message(f"{멤버.mention}님에게 방장을 넘겼어요.", ephemeral=True)
    await transfer_owner(ch, 멤버)


@room_group.command(name="정보", description="지금 있는 통화방(또는 내 방) 정보를 보여줘요")
async def cmd_info(interaction: discord.Interaction):
    member = interaction.user
    ch = None
    if member.voice and member.voice.channel and store.room(member.voice.channel.id):
        ch = member.voice.channel
    ch = ch or existing_room(interaction.guild, member)
    if not ch:
        await interaction.response.send_message("봇이 만든 통화방에 들어가 있거나 내 방이 있어야 볼 수 있어요.", ephemeral=True)
        return
    room = store.room(ch.id)
    embed = discord.Embed(title=ch.name, color=discord.Color.blurple())
    embed.add_field(name="방장", value=f"<@{room['owner']}>")
    embed.add_field(name="인원", value=f"{len(ch.members)} / {ch.user_limit or '제한 없음'}")
    embed.add_field(name="상태", value="🔒 잠금" if room["locked"] else "🔓 공개")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────── /통화방설정 (관리자) ───────────────────────────────
setup_group = app_commands.Group(
    name="통화방설정", description="통화방 생성 채널 설정 (관리자)", guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@setup_group.command(name="만들기", description="'통화방' 카테고리와 '➕ 통화방 만들기' 채널을 새로 만들어요")
async def cmd_setup_make(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    category = await guild.create_category("통화방", reason="통화방 봇 설정")
    hub = await guild.create_voice_channel("➕ 통화방 만들기", category=category, reason="통화방 봇 설정")
    store.set_hub(guild.id, hub.id)
    await interaction.followup.send(f"{hub.mention} 만들었어요! 여기 들어가면 개인 통화방이 생겨요.", ephemeral=True)


@setup_group.command(name="지정", description="이미 있는 음성 채널을 생성 채널로 지정해요")
async def cmd_setup_set(interaction: discord.Interaction, 채널: discord.VoiceChannel):
    store.set_hub(interaction.guild.id, 채널.id)
    await interaction.response.send_message(f"{채널.mention}을(를) 생성 채널로 지정했어요. 새 방은 같은 카테고리에 생겨요.", ephemeral=True)


@setup_group.command(name="해제", description="생성 채널 지정을 해제해요 (채널은 지우지 않아요)")
async def cmd_setup_clear(interaction: discord.Interaction):
    store.set_hub(interaction.guild.id, None)
    await interaction.response.send_message("생성 채널 지정을 해제했어요.", ephemeral=True)


@setup_group.command(name="구인채널", description="구인 글이 올라갈 채팅 채널을 정해요 (안 정하면 명령어 친 채널)")
async def cmd_setup_recruit(interaction: discord.Interaction, 채널: discord.TextChannel | None = None):
    store.set_recruit_channel(interaction.guild.id, 채널.id if 채널 else None)
    msg = f"구인 글은 이제 {채널.mention}에 올라가요." if 채널 else "구인 채널 지정을 해제했어요. 명령어 친 채널에 올라가요."
    await interaction.response.send_message(msg, ephemeral=True)


# ─────────────────────────────── /구인 ───────────────────────────────
@bot.tree.command(name="구인", description="지금 있는 통화방으로 같이 할 사람을 구해요")
@app_commands.guild_only()
@app_commands.describe(
    내용="예: 발로 경쟁 같이 하실 분, 골드~플래",
    인원="몇 명 더 구할지",
    게임="게임 이름 (선택)",
    멘션역할="알림 보낼 역할 (선택)",
)
async def cmd_recruit(interaction: discord.Interaction, 내용: app_commands.Range[str, 1, 300],
                      인원: app_commands.Range[int, 1, 20], 게임: app_commands.Range[str, 1, 50] | None = None,
                      멘션역할: discord.Role | None = None):
    member = interaction.user
    voice = member.voice.channel if member.voice and member.voice.channel else existing_room(interaction.guild, member)
    if voice is None:
        await interaction.response.send_message("음성 채널에 들어가 있거나 내 통화방이 있어야 구인할 수 있어요.", ephemeral=True)
        return

    target_id = store.recruit_channel(interaction.guild.id)
    target = interaction.guild.get_channel(target_id) if target_id else interaction.channel
    if target is None or not hasattr(target, "send"):
        await interaction.response.send_message("구인 글을 올릴 채널을 찾지 못했어요. 관리자에게 `/통화방설정 구인채널`을 부탁해 주세요.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    # 같은 통화방의 이전 구인 글은 마감
    await close_post(voice.id)

    current = len(humans(voice))
    info = {"voice": voice.id, "voice_name": voice.name, "author": member.id,
            "author_name": member.display_name, "content": 내용, "game": 게임, "target": current + 인원}
    header = f"{멘션역할.mention} " if 멘션역할 else ""
    try:
        msg = await target.send(
            content=f"{header}{member.mention}님이 {voice.mention}에서 같이 할 사람을 구해요!",
            file=await recruit_card_file(info, voice),
            view=recruit_view(info, voice.jump_url),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
    except discord.HTTPException:
        await interaction.followup.send(f"{target.mention}에 글을 올리지 못했어요. 봇 권한을 확인해 주세요.", ephemeral=True)
        return
    info.update(channel=target.id, message=msg.id)
    store.set_post(voice.id, **info)
    await interaction.followup.send(f"구인 글을 올렸어요: {msg.jump_url}", ephemeral=True)


@bot.tree.command(name="구인마감", description="내가 올린 구인 글을 마감해요")
@app_commands.guild_only()
async def cmd_recruit_close(interaction: discord.Interaction):
    mine = [int(v) for v, p in store.data["posts"].items() if p["author"] == interaction.user.id]
    if not mine:
        await interaction.response.send_message("열려 있는 내 구인 글이 없어요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    for vid in mine:
        await close_post(vid)
    await interaction.followup.send("구인을 마감했어요.", ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("명령어 오류", exc_info=error)
    msg = "문제가 생겼어요. 봇 권한(채널 관리·역할 관리·멤버 이동)을 확인해 주세요."
    if isinstance(error, app_commands.MissingPermissions):
        msg = "이 명령어는 서버 관리 권한이 필요해요."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


bot.tree.add_command(room_group)
bot.tree.add_command(setup_group)

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN 환경변수가 없어요. .env 파일을 확인해 주세요.")
    bot.run(TOKEN, log_handler=None)
