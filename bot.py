"""
통화방 자동 생성 디스코드 봇 (Join to Create)

- 생성 채널에 들어가면 "OOO의 통화방"을 만들어 자동 이동
- 게임별 생성 채널: "➕ 발로란트 5인방"에 들어가면 "OOO의 발로란트"(인원 5명 제한)
- /통화방 생성 명령어로도 생성 가능
- 방이 생기면 음성 채널 채팅에 버튼 컨트롤 패널 (잠금·공개·허용·이름·인원·방장넘기기)
- 빈 방 자동 삭제, 방장이 나가면 자동 위임, 한 사람당 방 1개
- /구인: 지금 있는 통화방으로 사람 구하는 이미지 글 (입장 버튼, 인원 자동 갱신, 방 비면 자동 마감)
- 게임은 관리자가 등록한 것만 쓸 수 있고, 명령어에서 자동완성으로 고름
"""

import asyncio
import hashlib
import io
import json
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import recruit_card

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATA_FILE = os.getenv("DATA_FILE", "data.json")
ICON_DIR = os.getenv("ICON_DIR", "icons")
# 명령어로 만든 방에 아무도 안 들어오면 이 시간(초) 뒤 삭제
EMPTY_GRACE_SECONDS = int(os.getenv("EMPTY_GRACE_SECONDS", "60"))
# 구인 글 자동 마감까지 시간(시간 단위, 0이면 자동 마감 안 함)
RECRUIT_EXPIRE_HOURS = float(os.getenv("RECRUIT_EXPIRE_HOURS", "3"))
TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", "Asia/Seoul"))
# 디스코드는 채널 이름 변경을 10분에 2번으로 제한함 → 오래 기다리지 않고 포기할 시간
RENAME_TIMEOUT = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("voicebot")


def game_key(name: str) -> str:
    """'League of Legends', 'league of legends' 를 같은 게임으로 보기."""
    return "".join(name.lower().split())


# ─────────────────────────────── 저장소 ───────────────────────────────
class Store:
    """
    data.json 구조
    guilds[서버ID]:
        hubs:   {생성채널ID: {"game": 게임 이름 또는 None, "limit": 인원 제한}}
        games:  {게임키: {"name": 표시 이름, "file": 아이콘 파일 경로 또는 None}}
        recruit: 구인 글 채널ID
    rooms[방ID]: {"owner", "guild", "custom_name", "locked", "game"}
    posts[음성채널ID]: 구인 글 정보
    """

    def __init__(self, path: str):
        self.path = path
        self.data = {"guilds": {}, "rooms": {}, "posts": {}}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        for key in ("guilds", "rooms", "posts"):
            self.data.setdefault(key, {})
        self._migrate()

    def _migrate(self):
        """이전 버전 data.json(생성 채널 1개, game_icons) 자동 변환."""
        changed = False
        for g in self.data["guilds"].values():
            if "hub" in g:
                hub = g.pop("hub")
                if hub:
                    g.setdefault("hubs", {})[str(hub)] = {"game": None, "limit": 0}
                changed = True
            if "game_icons" in g:
                g.setdefault("games", {}).update(g.pop("game_icons"))
                changed = True
        if changed:
            self.save()

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def guild(self, guild_id: int) -> dict:
        return self.data["guilds"].setdefault(str(guild_id), {})

    # 생성 채널
    def hubs(self, guild_id: int) -> dict:
        return self.guild(guild_id).get("hubs", {})

    def set_hub(self, guild_id: int, channel_id: int, game=None, limit=0):
        self.guild(guild_id).setdefault("hubs", {})[str(channel_id)] = {"game": game, "limit": limit}
        self.save()

    def remove_hub(self, guild_id: int, channel_id: int) -> bool:
        removed = self.guild(guild_id).get("hubs", {}).pop(str(channel_id), None) is not None
        if removed:
            self.save()
        return removed

    # 방
    def room(self, channel_id: int):
        return self.data["rooms"].get(str(channel_id))

    def add_room(self, channel_id: int, owner_id: int, guild_id: int, custom_name: bool, game=None):
        self.data["rooms"][str(channel_id)] = {
            "owner": owner_id,
            "guild": guild_id,
            "custom_name": custom_name,  # 방장이 직접 이름을 정했으면 방장 바뀌어도 이름 유지
            "locked": False,
            "game": game,
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
        return self.guild(guild_id).get("recruit")

    def set_recruit_channel(self, guild_id: int, channel_id):
        self.guild(guild_id)["recruit"] = channel_id
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

    # 등록된 게임
    def games(self, guild_id: int) -> dict:
        return self.guild(guild_id).get("games", {})

    def game(self, guild_id: int, name: str | None):
        return self.games(guild_id).get(game_key(name)) if name else None

    def set_game(self, guild_id: int, name: str, icon_file):
        self.guild(guild_id).setdefault("games", {})[game_key(name)] = {"name": name, "file": icon_file}
        self.save()

    def remove_game(self, guild_id: int, name: str):
        self.guild(guild_id).get("games", {}).pop(game_key(name), None)
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


async def delete_room(channel: discord.VoiceChannel, reason: str):
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


def pick_category(guild: discord.Guild, member: discord.Member, fallback):
    """새 방을 만들 카테고리: 생성 채널 카테고리 > 지금 있는 음성 채널 카테고리 > 명령어 친 채널 카테고리."""
    category = first_hub_category(guild)
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
    asyncio.create_task(send_panel(channel, member))
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
    await notify(channel, f"👑 {new_owner.mention}님이 새 방장이 됐어요. 컨트롤 패널 버튼을 쓸 수 있어요.")
    if room and not room["custom_name"]:
        # 이름 변경은 제한이 있어서 기다리지 않게 백그라운드로
        asyncio.create_task(try_rename(channel, default_name(new_owner, room.get("game"))))


async def schedule_empty_check(channel_id: int):
    """명령어로 만들고 아무도 안 들어온 방 정리."""
    await asyncio.sleep(EMPTY_GRACE_SECONDS)
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


async def unlock_room(ch: discord.VoiceChannel):
    # 카테고리 기본 권한으로 되돌림
    await ch.edit(overwrites=dict(ch.category.overwrites) if ch.category else {})
    store.update_room(ch.id, locked=False)


async def allow_member(ch: discord.VoiceChannel, member: discord.Member, by) -> str:
    await ch.set_permissions(member, connect=True, reason=f"{by} 허용")
    extra = "" if store.room(ch.id)["locked"] else " (지금은 공개 상태라 원래 누구나 들어올 수 있어요)"
    return f"{member.mention}님이 {ch.mention}에 들어올 수 있어요.{extra}"


async def rename_room(ch: discord.VoiceChannel, name: str) -> str:
    if await try_rename(ch, name):
        store.update_room(ch.id, custom_name=True)
        return f"이름을 **{name}**(으)로 바꿨어요."
    return "디스코드 제한으로 채널 이름은 10분에 2번까지만 바꿀 수 있어요. 잠시 뒤에 다시 해 주세요."


async def set_limit(ch: discord.VoiceChannel, limit: int) -> str:
    await ch.edit(user_limit=limit)
    return "인원 제한을 없앴어요." if limit == 0 else f"인원을 **{limit}명**으로 제한했어요."


def transfer_problem(ch: discord.VoiceChannel, actor: discord.Member, target: discord.Member):
    """방장 넘기기가 안 되는 이유 (되면 None)."""
    if target.bot or target.id == actor.id:
        return "다른 사람(봇 제외)에게만 넘길 수 있어요."
    if target not in ch.members:
        return f"{target.mention}님이 {ch.mention}에 있어야 넘길 수 있어요."
    if store.room_of_owner(ch.guild.id, target.id) is not None:
        return "그 사람은 이미 다른 방의 방장이에요."
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
        title="🎛️ 통화방 컨트롤 패널",
        description=(
            f"방장: {owner.mention}\n\n"
            "🔒 **잠금** 지금 있는 사람만 들어올 수 있게\n"
            "🔓 **공개** 누구나 들어올 수 있게\n"
            "✅ **허용** 잠긴 방에 들어올 사람 고르기\n"
            "✏️ **이름** · 👥 **인원** · 👑 **방장넘기기**\n\n"
            "방장만 누를 수 있어요."
        ),
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
            await interaction.response.send_message("방장만 바꿀 수 있어요.", ephemeral=True)
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
            await interaction.response.send_message("방장만 바꿀 수 있어요.", ephemeral=True)
            return
        value = self.limit.value.strip()
        if not value.isdigit() or not 0 <= int(value) <= 99:
            await interaction.response.send_message("0부터 99 사이 숫자로 적어 주세요.", ephemeral=True)
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
            await interaction.response.edit_message(content="이제 방장이 아니라서 할 수 없어요.", view=None)
            return
        target = self.select.values[0]
        if not isinstance(target, discord.Member):
            target = interaction.guild.get_member(target.id)
        if target is None:
            await interaction.response.edit_message(content="그 사람을 찾지 못했어요.", view=None)
            return
        if self.mode == "allow":
            await interaction.response.edit_message(
                content=await allow_member(self.channel, target, interaction.user), view=None)
        else:
            problem = transfer_problem(self.channel, interaction.user, target)
            if problem:
                await interaction.response.edit_message(content=problem, view=None)
                return
            await interaction.response.edit_message(content=f"{target.mention}님에게 방장을 넘겼어요.", view=None)
            await transfer_owner(self.channel, target)


def is_owner(channel: discord.VoiceChannel, user) -> bool:
    room = store.room(channel.id)
    return bool(room) and room["owner"] == user.id


async def handle_panel(interaction: discord.Interaction, action: str, channel_id: int):
    ch = interaction.guild.get_channel(channel_id)
    if ch is None or not store.room(channel_id):
        await interaction.response.send_message("이미 사라진 통화방이에요.", ephemeral=True)
        return
    if not is_owner(ch, interaction.user):
        await interaction.response.send_message(f"방장(<@{store.room(channel_id)['owner']}>)만 누를 수 있어요.",
                                                ephemeral=True)
        return
    if action == "lock":
        await interaction.response.defer(ephemeral=True)
        await lock_room(ch, interaction.user)
        await interaction.followup.send("🔒 잠갔어요. 친구는 ✅ 허용으로 들일 수 있어요.", ephemeral=True)
    elif action == "unlock":
        await interaction.response.defer(ephemeral=True)
        await unlock_room(ch)
        await interaction.followup.send("🔓 공개로 바꿨어요.", ephemeral=True)
    elif action == "rename":
        await interaction.response.send_modal(RenameModal(ch))
    elif action == "limit":
        await interaction.response.send_modal(LimitModal(ch))
    elif action in ("allow", "transfer"):
        label = "누구를 들어오게 할까요?" if action == "allow" else "누구에게 방장을 넘길까요?"
        await interaction.response.send_message(label, view=PickMemberView(ch, action), ephemeral=True)


# ─────────────────────────────── 구인구직 ───────────────────────────────
async def _avatar_bytes(member: discord.Member):
    try:
        asset = member.display_avatar.replace(size=128, static_format="png")
        return await asyncio.wait_for(asset.read(), timeout=5)
    except Exception:
        return None


def expire_text(info: dict):
    if not info.get("expires_at"):
        return None
    t = datetime.fromtimestamp(info["expires_at"], TZ)
    ampm = "오전" if t.hour < 12 else "오후"
    hour = t.hour % 12 or 12
    return f"{ampm} {hour}:{t.minute:02d}에 자동 마감돼요"


async def _applicant_entry(guild: discord.Guild, applicant: dict):
    member = guild.get_member(applicant["id"])
    if member is None:
        try:
            member = await guild.fetch_member(applicant["id"])
        except discord.HTTPException:
            return applicant["name"], None
    return member.display_name, await _avatar_bytes(member)


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
        status=status,
    )
    return discord.File(buf, filename="recruit.png")


def recruit_view(info: dict, jump_url: str | None) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if jump_url:
        view.add_item(discord.ui.Button(label="🔊 통화방 들어가기", style=discord.ButtonStyle.link, url=jump_url))
    view.add_item(discord.ui.Button(label="참가할게요", emoji="🙋", style=discord.ButtonStyle.primary,
                                    custom_id=f"recruit_apply:{info['voice']}"))
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


async def handle_recruit_close(interaction: discord.Interaction, voice_id: int):
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


async def handle_recruit_apply(interaction: discord.Interaction, voice_id: int):
    """참가할게요 버튼: 누르면 신청, 다시 누르면 취소."""
    info = store.post(voice_id)
    if not info:
        await interaction.response.send_message("이미 마감된 구인 글이에요.", ephemeral=True)
        return
    user = interaction.user
    voice = bot.get_channel(voice_id)
    if user.id == info["author"]:
        await interaction.response.send_message("구인자는 신청하지 않아도 돼요.", ephemeral=True)
        return
    if voice and user in voice.members:
        await interaction.response.send_message("이미 통화방에 들어가 있어요.", ephemeral=True)
        return
    applicants = info.setdefault("applicants", [])
    if any(a["id"] == user.id for a in applicants):
        info["applicants"] = [a for a in applicants if a["id"] != user.id]
        store.set_post(voice_id, **info)
        await interaction.response.send_message("참가 신청을 취소했어요.", ephemeral=True)
    else:
        applicants.append({"id": user.id, "name": user.display_name})
        store.set_post(voice_id, **info)
        await interaction.response.send_message(
            "참가 신청했어요! 준비되면 '통화방 들어가기'를 눌러 주세요. (다시 누르면 취소)", ephemeral=True)
        if voice:
            try:
                await voice.send(f"🙋 {user.mention}님이 참가 신청했어요! <@{info['author']}>",
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


# ─────────────────────────────── 이벤트 ───────────────────────────────
@bot.event
async def on_ready():
    if not expire_posts.is_running():
        expire_posts.start()
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
    hub = store.hubs(guild.id).get(str(after.channel.id)) if after.channel else None
    if hub is not None:
        async with user_lock(member.id):
            room = existing_room(guild, member)
            created = room is None
            if created:
                try:
                    room = await create_room(member, after.channel.category,
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
    """패널·구인 마감 버튼 (봇이 재시작돼도 동작하도록 custom_id로 처리)."""
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = (interaction.data or {}).get("custom_id", "")
    try:
        if custom_id.startswith("recruit_apply:"):
            await handle_recruit_apply(interaction, int(custom_id.split(":")[1]))
        elif custom_id.startswith("recruit_close:"):
            await handle_recruit_close(interaction, int(custom_id.split(":")[1]))
        elif custom_id.startswith("panel:"):
            _, action, cid = custom_id.split(":")
            await handle_panel(interaction, action, int(cid))
    except discord.HTTPException as e:
        log.warning("버튼 처리 실패 %s: %s", custom_id, e)
        if not interaction.response.is_done():
            await interaction.response.send_message("처리하지 못했어요. 봇 권한을 확인해 주세요.", ephemeral=True)


@bot.event
async def on_guild_channel_delete(channel):
    store.remove_room(channel.id)
    await close_post(channel.id)
    store.remove_hub(channel.guild.id, channel.id)
    if store.recruit_channel(channel.guild.id) == channel.id:
        store.set_recruit_channel(channel.guild.id, None)


# ─────────────────────────────── 게임 자동완성 ───────────────────────────────
async def game_autocomplete(interaction: discord.Interaction, current: str):
    names = [e["name"] for e in store.games(interaction.guild_id).values()]
    key = game_key(current)
    return [app_commands.Choice(name=n, value=n) for n in sorted(names) if key in game_key(n)][:25]


async def resolve_game(interaction: discord.Interaction, name: str | None):
    """등록된 게임이면 표시 이름을 돌려주고, 아니면 안내 후 False."""
    if not name:
        return None
    entry = store.game(interaction.guild.id, name)
    if entry:
        return entry["name"]
    names = ", ".join(sorted(e["name"] for e in store.games(interaction.guild.id).values())) or "아직 없음"
    msg = (f"**{name}**은(는) 등록된 게임이 아니에요. 목록에서 골라 주세요.\n"
           f"등록된 게임: {names}\n(관리자는 `/통화방설정 게임추가`로 등록할 수 있어요)")
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    return False


# ─────────────────────────────── /통화방 ───────────────────────────────
room_group = app_commands.Group(name="통화방", description="통화방 만들기와 관리", guild_only=True)


async def owned_room(interaction: discord.Interaction):
    """명령어 친 사람이 방장인 방. 없으면 안내하고 None."""
    ch = existing_room(interaction.guild, interaction.user)
    if ch is None:
        await interaction.response.send_message("방장인 통화방이 없어요. `/통화방 생성`으로 먼저 만들어 주세요.", ephemeral=True)
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
            await interaction.response.send_message(f"이미 방이 있어요: {ch.mention}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        category = pick_category(interaction.guild, member, interaction.channel)
        try:
            ch = await create_room(member, category, name=이름, limit=인원, game=game)
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
    await interaction.followup.send("🔒 잠갔어요. 친구를 들이려면 `/통화방 허용`을 써 주세요.", ephemeral=True)


@room_group.command(name="공개", description="잠금을 풀어서 누구나 들어올 수 있게 해요")
async def cmd_unlock(interaction: discord.Interaction):
    ch = await owned_room(interaction)
    if not ch:
        return
    await interaction.response.defer(ephemeral=True)
    await unlock_room(ch)
    await interaction.followup.send("🔓 공개로 바꿨어요.", ephemeral=True)


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
    await interaction.response.send_message(f"{멤버.mention}님에게 방장을 넘겼어요.", ephemeral=True)
    await transfer_owner(ch, 멤버)


@room_group.command(name="패널", description="내 통화방 컨트롤 패널을 다시 올려요")
async def cmd_panel(interaction: discord.Interaction):
    ch = await owned_room(interaction)
    if not ch:
        return
    await send_panel(ch, interaction.user)
    await interaction.response.send_message(f"{ch.mention} 채팅에 패널을 올렸어요.", ephemeral=True)


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
    if room.get("game"):
        embed.add_field(name="게임", value=room["game"])
    await interaction.response.send_message(embed=embed, ephemeral=True)


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
    await interaction.followup.send(f"{hub.mention} 만들었어요! 여기 들어가면 개인 통화방이 생겨요.", ephemeral=True)


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
    await interaction.followup.send(f"{hub.mention} 만들었어요! 들어가면 'OOO의 {game}' 방이 생겨요 ({limit_text}).",
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
    await interaction.response.send_message(f"{채널.mention}을(를) 생성 채널로 지정했어요. 새 방은 같은 카테고리에 생겨요.",
                                            ephemeral=True)


@setup_group.command(name="해제", description="생성 채널 지정을 해제해요 (채널은 지우지 않아요)")
async def cmd_setup_clear(interaction: discord.Interaction, 채널: discord.VoiceChannel):
    if store.remove_hub(interaction.guild.id, 채널.id):
        await interaction.response.send_message(f"{채널.mention} 생성 채널 지정을 해제했어요.", ephemeral=True)
    else:
        await interaction.response.send_message(f"{채널.mention}은(는) 생성 채널이 아니에요.", ephemeral=True)


@setup_group.command(name="목록", description="생성 채널·등록된 게임·구인 채널을 보여줘요")
async def cmd_setup_list(interaction: discord.Interaction):
    gid = interaction.guild.id
    hub_lines = []
    for cid, cfg in store.hubs(gid).items():
        detail = cfg.get("game") or "일반"
        if cfg.get("limit"):
            detail += f", {cfg['limit']}명"
        hub_lines.append(f"<#{cid}> ({detail})")
    game_lines = [f"{e['name']}{' 🖼️' if e.get('file') else ''}" for e in store.games(gid).values()]
    recruit = store.recruit_channel(gid)
    embed = discord.Embed(title="통화방 봇 설정", color=discord.Color.blurple())
    embed.add_field(name="생성 채널", value="\n".join(hub_lines) or "없음 (`/통화방설정 만들기`)", inline=False)
    embed.add_field(name="등록된 게임 (🖼️ = 아이콘 있음)",
                    value=", ".join(sorted(game_lines)) or "없음 (`/통화방설정 게임추가`)", inline=False)
    embed.add_field(name="구인 채널", value=f"<#{recruit}>" if recruit else "명령어 친 채널", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@setup_group.command(name="구인채널", description="구인 글이 올라갈 채팅 채널을 정해요 (안 정하면 명령어 친 채널)")
async def cmd_setup_recruit(interaction: discord.Interaction, 채널: discord.TextChannel | None = None):
    store.set_recruit_channel(interaction.guild.id, 채널.id if 채널 else None)
    msg = f"구인 글은 이제 {채널.mention}에 올라가요." if 채널 else "구인 채널 지정을 해제했어요. 명령어 친 채널에 올라가요."
    await interaction.response.send_message(msg, ephemeral=True)


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
        await interaction.response.send_message(f"**{name}** 게임을 등록했어요.", ephemeral=True)
        return
    if not (아이콘.content_type or "").startswith("image/") or 아이콘.size > 5 * 1024 * 1024:
        await interaction.response.send_message("아이콘은 5MB 이하 이미지 파일(PNG/JPG)만 올릴 수 있어요.", ephemeral=True)
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
        await interaction.followup.send("이미지를 읽지 못했어요. 다른 파일로 해 주세요.", ephemeral=True)
        return
    store.set_game(gid, name, path)
    await interaction.followup.send(f"**{name}** 게임을 아이콘과 함께 등록했어요.", ephemeral=True)


@setup_group.command(name="게임삭제", description="등록한 게임을 지워요")
@app_commands.autocomplete(게임=game_autocomplete)
async def cmd_setup_game_remove(interaction: discord.Interaction, 게임: str):
    entry = store.game(interaction.guild.id, 게임)
    if not entry:
        await interaction.response.send_message("등록된 게임이 아니에요.", ephemeral=True)
        return
    if entry.get("file") and os.path.exists(entry["file"]):
        os.remove(entry["file"])
    store.remove_game(interaction.guild.id, 게임)
    await interaction.response.send_message(
        f"**{entry['name']}** 게임을 지웠어요. 이 게임으로 만든 생성 채널이 있으면 `/통화방설정 해제`로 정리해 주세요.",
        ephemeral=True)


# ─────────────────────────────── /구인 ───────────────────────────────
# 게임별 티어 자동완성 추천 (직접 입력도 가능, 예: 골드~플래)
GAME_PRESETS = [
    (("롤", "리그오브레전드", "leagueoflegends", "lol"),
     ["아이언", "브론즈", "실버", "골드", "플래티넘", "에메랄드", "다이아몬드", "마스터", "그랜드마스터", "챌린저"]),
    (("발로", "valorant"),
     ["아이언", "브론즈", "실버", "골드", "플래티넘", "다이아몬드", "초월자", "불멸", "레디언트"]),
    (("옵치", "오버워치", "overwatch"),
     ["브론즈", "실버", "골드", "플래티넘", "다이아몬드", "마스터", "그랜드마스터", "챔피언"]),
]


def tiers_for(game: str | None):
    key = game_key(game or "")
    for names, tiers in GAME_PRESETS:
        if any(n in key for n in names):
            return tiers
    return []


def current_game(interaction: discord.Interaction):
    game = getattr(interaction.namespace, "게임", None)
    if game:
        return game
    member = interaction.user
    if getattr(member, "voice", None) and member.voice.channel and store.room(member.voice.channel.id):
        return store.room(member.voice.channel.id).get("game")
    return None


def suggest(options: list[str], current: str):
    current = current.strip()
    choices = []
    if "~" in current:  # 범위 입력: "골드~" → "골드~플래티넘" ...
        head, tail = current.split("~", 1)
        higher = options[options.index(head) + 1:] if head in options else options
        choices = [f"{head}~{o}" for o in higher if game_key(tail) in game_key(o)]
    else:
        choices = [o for o in options if game_key(current) in game_key(o)]
    if current and current not in choices and not current.endswith("~"):
        choices.append(current)  # 목록에 없는 것도 직접 입력 가능
    return [app_commands.Choice(name=c[:100], value=c[:30]) for c in choices][:25]


async def tier_autocomplete(interaction: discord.Interaction, current: str):
    return suggest(tiers_for(current_game(interaction)), current)


@bot.tree.command(name="구인", description="지금 있는 통화방으로 같이 할 사람을 구해요")
@app_commands.guild_only()
@app_commands.describe(
    내용="예: 경쟁 같이 하실 분, 마이크 필수",
    인원="몇 명 더 구할지",
    게임="등록된 게임 중에서 고르기 (게임 방이면 자동으로 채워져요)",
    티어="예: 골드, 골드~플래 (선택)",
    멘션역할="알림 보낼 역할 (선택)",
)
@app_commands.autocomplete(게임=game_autocomplete, 티어=tier_autocomplete)
async def cmd_recruit(interaction: discord.Interaction, 내용: app_commands.Range[str, 1, 300],
                      인원: app_commands.Range[int, 1, 20], 게임: str | None = None,
                      티어: app_commands.Range[str, 1, 30] | None = None,
                      멘션역할: discord.Role | None = None):
    member = interaction.user
    voice = member.voice.channel if member.voice and member.voice.channel else existing_room(interaction.guild, member)
    if voice is None:
        await interaction.response.send_message("음성 채널에 들어가 있거나 내 통화방이 있어야 구인할 수 있어요.", ephemeral=True)
        return
    game = await resolve_game(interaction, 게임)
    if game is False:
        return
    if game is None and store.room(voice.id):  # 게임 방이면 그 게임으로
        game = store.room(voice.id).get("game")

    target_id = store.recruit_channel(interaction.guild.id)
    target = interaction.guild.get_channel(target_id) if target_id else interaction.channel
    if target is None or not hasattr(target, "send"):
        await interaction.response.send_message("구인 글을 올릴 채널을 찾지 못했어요. 관리자에게 `/통화방설정 구인채널`을 부탁해 주세요.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    # 같은 통화방의 이전 구인 글은 마감
    await close_post(voice.id)

    current = len(humans(voice))
    info = {"voice": voice.id, "voice_name": voice.name, "author": member.id, "guild": interaction.guild.id,
            "author_name": member.display_name, "content": 내용, "game": game, "target": current + 인원,
            "tags": [티어.strip()] if 티어 and 티어.strip() else [], "applicants": [],
            "expires_at": time.time() + RECRUIT_EXPIRE_HOURS * 3600 if RECRUIT_EXPIRE_HOURS > 0 else None}
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
