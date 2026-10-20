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
import random
import re
import shutil
import time
from datetime import datetime, time as dtime, timedelta
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
        self.data = {"guilds": {}, "rooms": {}, "posts": {}, "menus": {}, "reservations": {}, "giveaways": {}}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        for key in ("guilds", "rooms", "posts", "menus", "reservations", "giveaways"):
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

    # 방이 생길 카테고리 (서버 기본값)
    def room_category(self, guild_id: int):
        return self.guild(guild_id).get("room_category")

    def set_room_category(self, guild_id: int, category_id):
        self.guild(guild_id)["room_category"] = category_id
        self.save()

    def set_hub_category(self, guild_id: int, hub_id: int, category_id) -> bool:
        hub = self.hubs(guild_id).get(str(hub_id))
        if hub is None:
            return False
        hub["category"] = category_id
        self.save()
        return True

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
        games = self.guild(guild_id).setdefault("games", {})
        entry = games.setdefault(game_key(name), {})
        entry.update(name=name, file=icon_file)  # 연결된 역할 등 다른 설정은 유지
        self.save()

    def set_game_role(self, guild_id: int, name: str, role_id):
        self.games(guild_id)[game_key(name)]["role"] = role_id
        self.save()

    def remove_game(self, guild_id: int, name: str):
        self.guild(guild_id).get("games", {}).pop(game_key(name), None)
        self.save()

    # 역할 메뉴 {메시지ID: {"guild", "channel", "title", "desc", "items": {이모지키: {"emoji", "role"}}}}
    def menu(self, message_id: int):
        return self.data["menus"].get(str(message_id))

    def set_menu(self, message_id: int, menu: dict):
        self.data["menus"][str(message_id)] = menu
        self.save()

    def remove_menu(self, message_id: int):
        if self.data["menus"].pop(str(message_id), None) is not None:
            self.save()

    # 예약 {메시지ID: {...}}
    def res(self, message_id):
        return self.data["reservations"].get(str(message_id))

    def set_res(self, message_id, res: dict):
        self.data["reservations"][str(message_id)] = res
        self.save()

    def remove_res(self, message_id):
        if self.data["reservations"].pop(str(message_id), None) is not None:
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
    await notify(channel, f"👑 {new_owner.mention}님이 방장이 됐어요.")
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
        await interaction.followup.send("🔒 잠갔어요.", ephemeral=True)
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
    return f"{ampm} {hour}:{t.minute:02d} 마감"


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


def recruit_view(info: dict, jump_url: str | None) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if jump_url:
        view.add_item(discord.ui.Button(label="🔊 통화방 들어가기", style=discord.ButtonStyle.link, url=jump_url))
    view.add_item(discord.ui.Button(label="참가", style=discord.ButtonStyle.primary,
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
    """참가 버튼: 누르면 신청, 다시 누르면 취소."""
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
            "참가 신청했어요.", ephemeral=True)
        if voice:
            try:
                await voice.send(f"{user.mention}님이 참가 신청했어요. <@{info['author']}>",
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
            await delete_room(ch, "봇 재시작 후 빈 통화방 정리")
    for vid in list(store.data["posts"].keys()):
        await refresh_post(int(vid))
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
    """패널·구인 마감 버튼 (봇이 재시작돼도 동작하도록 custom_id로 처리)."""
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
            await interaction.response.send_message("처리하지 못했어요. 봇 권한을 확인해 주세요.", ephemeral=True)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    await apply_menu_reaction(payload, add=True)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    await apply_menu_reaction(payload, add=False)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    store.remove_menu(payload.message_id)
    store.remove_res(payload.message_id)
    store.data["giveaways"].pop(str(payload.message_id), None)


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


# ─────────────────────────────── 게임 자동완성 ───────────────────────────────
async def game_autocomplete(interaction: discord.Interaction, current: str):
    names = [e["name"] for e in store.games(interaction.guild_id).values()]
    key = game_key(current)
    return [app_commands.Choice(name=n, value=n) for n in sorted(names) if key in game_key(n)][:25]


def mention_ok(guild: discord.Guild, roles, channel=None) -> bool:
    """봇이 이 역할들을 실제로 멘션(알림)할 수 있는지."""
    perms = (channel or guild).permissions_for(guild.me) if channel else guild.me.guild_permissions
    return perms.mention_everyone or all(r.mentionable for r in roles)


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
    await interaction.followup.send("🔒 잠갔어요.", ephemeral=True)


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
    await interaction.followup.send(f"{hub.mention} 만들었어요.", ephemeral=True)


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
    await interaction.followup.send(f"{hub.mention} 만들었어요. ({limit_text})",
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
    await interaction.response.send_message(f"{채널.mention}을(를) 생성 채널로 지정했어요.",
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
    room_cat = store.room_category(gid)
    embed.add_field(name="방 카테고리 (기본)", value=f"📁 <#{room_cat}>" if room_cat else "생성 채널이 있는 카테고리",
                    inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@setup_group.command(name="카테고리", description="통화방이 만들어질 카테고리를 정해요")
@app_commands.describe(카테고리="방이 만들어질 카테고리 (비우면 기본값으로 되돌림)",
                       생성채널="이 생성 채널로 만든 방에만 적용 (비우면 서버 전체 기본값)")
async def cmd_setup_category(interaction: discord.Interaction, 카테고리: discord.CategoryChannel | None = None,
                             생성채널: discord.VoiceChannel | None = None):
    gid = interaction.guild.id
    cat_id = 카테고리.id if 카테고리 else None
    if 생성채널:
        if not store.set_hub_category(gid, 생성채널.id, cat_id):
            await interaction.response.send_message(f"{생성채널.mention}은(는) 생성 채널이 아니에요.", ephemeral=True)
            return
        msg = (f"{생성채널.mention}으로 만든 방은 이제 📁 **{카테고리.name}** 카테고리에 생겨요." if 카테고리
               else f"{생성채널.mention} 전용 카테고리를 해제했어요. 서버 기본 카테고리를 따라가요.")
    else:
        store.set_room_category(gid, cat_id)
        msg = (f"통화방은 이제 기본으로 📁 **{카테고리.name}** 카테고리에 생겨요." if 카테고리
               else "기본 카테고리를 해제했어요. 생성 채널이 있는 카테고리에 방이 생겨요.")
    if 카테고리:
        perms = 카테고리.permissions_for(interaction.guild.me)
        if not (perms.manage_channels and perms.move_members):
            msg += "\n⚠️ 봇이 이 카테고리에서 '채널 관리'나 '멤버 이동' 권한이 없어요. 카테고리 권한을 확인해 주세요."
    await interaction.response.send_message(msg, ephemeral=True)


@setup_group.command(name="구인채널", description="구인 글이 올라갈 채팅 채널을 정해요 (안 정하면 명령어 친 채널)")
async def cmd_setup_recruit(interaction: discord.Interaction, 채널: discord.TextChannel | None = None):
    store.set_recruit_channel(interaction.guild.id, 채널.id if 채널 else None)
    msg = f"구인 글은 이제 {채널.mention}에 올라가요." if 채널 else "구인 채널 지정을 해제했어요. 명령어 친 채널에 올라가요."
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
        await interaction.response.send_message(f"**{game}** 역할 연결을 해제했어요.", ephemeral=True)
        return
    msg = f"이제 **{game}** 구인 글이 올라오면 {역할.mention}이(가) 자동으로 멘션돼요."
    if not mention_ok(interaction.guild, [역할]):
        msg += ("\n⚠️ 지금은 봇이 이 역할을 멘션할 수 없어요. 서버 설정 → 역할 → 이 역할에서 "
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
        await interaction.response.send_message("서버를 목록에서 골라 주세요.", ephemeral=True)
        return
    try:
        member = source.get_member(interaction.user.id) or await source.fetch_member(interaction.user.id)
    except discord.HTTPException:
        member = None
    if member is None or not member.guild_permissions.manage_guild:
        await interaction.response.send_message(f"**{source.name}**에서 서버 관리 권한이 있어야 가져올 수 있어요.",
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
    await interaction.response.send_message("\n".join(lines) or "가져올 게임이 없어요.", ephemeral=True)


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

    current = len(humans(voice))
    if 인원 <= current:
        await interaction.response.send_message(
            f"지금 {voice.mention}에 이미 {current}명이 있어요. 인원은 {current + 1}명 이상으로 정해 주세요.", ephemeral=True)
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
    header = "".join(f"{r.mention} " for r in roles)
    try:
        msg = await target.send(
            content=f"{header}{member.mention} · {voice.mention}",
            file=await recruit_card_file(info, voice),
            view=recruit_view(info, voice.jump_url),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
    except discord.HTTPException:
        await interaction.followup.send(f"{target.mention}에 글을 올리지 못했어요. 봇 권한을 확인해 주세요.", ephemeral=True)
        return
    info.update(channel=target.id, message=msg.id)
    store.set_post(voice.id, **info)
    done = f"구인 글을 올렸어요: {msg.jump_url}"
    if roles and not mention_ok(interaction.guild, roles, target):
        done += "\n⚠️ 역할 멘션이 알림으로 가지 않았을 수 있어요. 관리자에게 그 역할의 '@멘션 허용' 설정을 부탁해 주세요."
    await interaction.followup.send(done, ephemeral=True)


# ─────────────────────────────── 역할 메뉴 (이모지 반응 → 역할) ───────────────────────────────
def emoji_key(emoji: discord.PartialEmoji) -> str:
    """서버 이모지는 ID, 기본 이모지는 글자 자체로 구분."""
    return str(emoji.id) if emoji.id else emoji.name


def menu_embed(menu: dict) -> discord.Embed:
    lines = [f"{item['emoji']}  →  <@&{item['role']}>" for item in menu["items"].values()]
    embed = discord.Embed(title=menu["title"], color=discord.Color.blurple(),
                          description=(menu.get("desc") + "\n\n" if menu.get("desc") else "")
                          + ("\n".join(lines) or "-"))
    return embed


async def apply_menu_reaction(payload: discord.RawReactionActionEvent, add: bool):
    if payload.guild_id is None or payload.user_id == bot.user.id:
        return
    menu = store.menu(payload.message_id)
    item = menu["items"].get(emoji_key(payload.emoji)) if menu else None
    if not item:
        return
    guild = bot.get_guild(payload.guild_id)
    role = guild.get_role(item["role"]) if guild else None
    if role is None:
        return
    member = payload.member if add else guild.get_member(payload.user_id)
    if member is None:
        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.HTTPException:
            return
    if member.bot:
        return
    try:
        if add and role not in member.roles:
            await member.add_roles(role, reason="역할 메뉴")
        elif not add and role in member.roles:
            await member.remove_roles(role, reason="역할 메뉴")
    except discord.HTTPException as e:
        log.warning("역할 메뉴 처리 실패 (%s): %s", role.name, e)


async def fetch_menu_message(guild: discord.Guild, message_id: int):
    menu = store.menu(message_id)
    ch = guild.get_channel(menu["channel"]) if menu else None
    if ch is None:
        return None
    try:
        return await ch.fetch_message(message_id)
    except discord.HTTPException:
        return None


async def menu_autocomplete(interaction: discord.Interaction, current: str):
    menus = [(mid, m) for mid, m in store.data["menus"].items() if m["guild"] == interaction.guild_id]
    key = game_key(current)
    return [app_commands.Choice(name=f"{m['title']} (#{getattr(interaction.guild.get_channel(m['channel']), 'name', '?')})"[:100],
                                value=mid) for mid, m in menus if key in game_key(m["title"])][:25]


async def menu_emoji_autocomplete(interaction: discord.Interaction, current: str):
    menu = store.menu(getattr(interaction.namespace, "메뉴", None) or 0)
    if not menu:
        return []
    out = []
    for item in menu["items"].values():
        role = interaction.guild.get_role(item["role"])
        label = f"{item['emoji'] if not item['emoji'].startswith('<') else ':' + item['emoji'].split(':')[1] + ':'} → {role.name if role else '?'}"
        out.append(app_commands.Choice(name=label[:100], value=item["emoji"]))
    return out[:25]


def role_problem(guild: discord.Guild, role: discord.Role):
    if role.is_default():
        return "@everyone은 넣을 수 없어요."
    if role.managed:
        return "봇·연동 전용 역할은 넣을 수 없어요."
    if role >= guild.me.top_role:
        return (f"{role.mention}이(가) 봇 역할보다 위에 있어서 줄 수 없어요. "
                "서버 설정 → 역할에서 봇 역할을 그 역할보다 위로 올려 주세요.")
    return None


menu_group = app_commands.Group(
    name="역할메뉴", description="이모지를 누르면 역할을 받는 메뉴 (관리자)", guild_only=True,
    default_permissions=discord.Permissions(manage_roles=True),
)


@menu_group.command(name="만들기", description="역할 메뉴 글을 올려요")
@app_commands.describe(제목="예: 게임 역할 받기", 설명="제목 아래 안내 문구 (선택)", 채널="올릴 채널 (비우면 지금 채널)")
async def cmd_menu_create(interaction: discord.Interaction, 제목: app_commands.Range[str, 1, 100],
                          설명: app_commands.Range[str, 1, 500] | None = None,
                          채널: discord.TextChannel | None = None):
    target = 채널 or interaction.channel
    menu = {"guild": interaction.guild.id, "channel": target.id, "title": 제목, "desc": 설명, "items": {}}
    try:
        msg = await target.send(embed=menu_embed(menu))
    except discord.HTTPException:
        await interaction.response.send_message(f"{target.mention}에 글을 올리지 못했어요. 봇 권한을 확인해 주세요.", ephemeral=True)
        return
    store.set_menu(msg.id, menu)
    await interaction.response.send_message(
        f"역할 메뉴를 올렸어요: {msg.jump_url}", ephemeral=True)


@menu_group.command(name="추가", description="역할 메뉴에 이모지와 역할을 넣어요 (같은 이모지면 역할을 바꿔요)")
@app_commands.describe(메뉴="역할 메뉴", 이모지="기본 이모지나 이 서버 이모지", 역할="받게 할 역할")
@app_commands.autocomplete(메뉴=menu_autocomplete)
async def cmd_menu_add(interaction: discord.Interaction, 메뉴: str, 이모지: str, 역할: discord.Role):
    menu = store.menu(int(메뉴)) if 메뉴.isdigit() else None
    if not menu:
        await interaction.response.send_message("역할 메뉴를 목록에서 골라 주세요.", ephemeral=True)
        return
    problem = role_problem(interaction.guild, 역할)
    if problem:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    msg = await fetch_menu_message(interaction.guild, int(메뉴))
    if msg is None:
        store.remove_menu(int(메뉴))
        await interaction.followup.send("역할 메뉴 글을 찾지 못했어요. 지워졌다면 `/역할메뉴 만들기`로 다시 만들어 주세요.", ephemeral=True)
        return
    emoji = discord.PartialEmoji.from_str(이모지.strip())
    if len(menu["items"]) >= 20 and emoji_key(emoji) not in menu["items"]:
        await interaction.followup.send("디스코드 제한으로 한 메뉴에 이모지는 20개까지예요.", ephemeral=True)
        return
    try:
        await msg.add_reaction(emoji)
    except discord.HTTPException:
        await interaction.followup.send("그 이모지를 쓸 수 없어요. 기본 이모지나 이 서버에 있는 이모지를 한 개만 넣어 주세요.",
                                        ephemeral=True)
        return
    menu["items"][emoji_key(emoji)] = {"emoji": str(emoji), "role": 역할.id}
    store.set_menu(msg.id, menu)
    await msg.edit(embed=menu_embed(menu))
    await interaction.followup.send(f"{emoji} → {역할.mention}", ephemeral=True,
                                    allowed_mentions=discord.AllowedMentions.none())


@menu_group.command(name="빼기", description="역할 메뉴에서 이모지를 빼요")
@app_commands.autocomplete(메뉴=menu_autocomplete, 이모지=menu_emoji_autocomplete)
async def cmd_menu_remove(interaction: discord.Interaction, 메뉴: str, 이모지: str):
    menu = store.menu(int(메뉴)) if 메뉴.isdigit() else None
    if not menu:
        await interaction.response.send_message("역할 메뉴를 목록에서 골라 주세요.", ephemeral=True)
        return
    emoji = discord.PartialEmoji.from_str(이모지.strip())
    if menu["items"].pop(emoji_key(emoji), None) is None:
        await interaction.response.send_message("그 메뉴에 없는 이모지예요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    store.set_menu(int(메뉴), menu)
    msg = await fetch_menu_message(interaction.guild, int(메뉴))
    if msg:
        await msg.edit(embed=menu_embed(menu))
        try:
            await msg.clear_reaction(emoji)
        except discord.HTTPException:
            pass
    await interaction.followup.send(f"{emoji} 을(를) 뺐어요.", ephemeral=True)


# ─────────────────────────────── /도움말 ───────────────────────────────
COMMAND_IDS: dict[str, int] = {}


def cmd(path: str) -> str:
    """'통화방 생성' → 누르면 바로 입력되는 명령어 링크 (ID를 모르면 그냥 글자)."""
    cid = COMMAND_IDS.get(path.split()[0])
    return f"</{path}:{cid}>" if cid else f"`/{path}`"


def help_embeds(is_admin: bool) -> list[discord.Embed]:
    room = discord.Embed(title="🎛️ 통화방", color=discord.Color.blurple(), description=(
        "**➕ 생성 채널**에 들어가면 내 통화방이 자동으로 생겨요. 방이 비면 사라지고, "
        "방장이 나가면 남은 사람에게 방장이 넘어가요. 한 사람당 방은 1개예요.\n\n"
        "방이 생기면 채팅에 **컨트롤 패널**이 올라와요. 방장은 버튼으로 "
        "🔒잠금 · 🔓공개 · ✅허용 · ✏️이름 · 👥인원 · 👑방장넘기기를 할 수 있어요."
    ))
    room.add_field(name="명령어", inline=False, value="\n".join([
        f"{cmd('통화방 생성')} 내 방 만들기 (이름·인원·게임 선택)",
        f"{cmd('통화방 이름')} 방 이름 바꾸기 (10분에 2번까지)",
        f"{cmd('통화방 인원')} 인원 제한 (0 = 제한 없음)",
        f"{cmd('통화방 잠금')} / {cmd('통화방 공개')} 잠그기 / 풀기",
        f"{cmd('통화방 허용')} 잠긴 방에 들어올 사람 정하기",
        f"{cmd('통화방 방장넘기기')} 같은 방 사람에게 방장 넘기기",
        f"{cmd('통화방 패널')} 컨트롤 패널 다시 올리기",
        f"{cmd('통화방 정보')} 방장·인원·상태 보기",
    ]))
    recruit = discord.Embed(title="📢 구인구직", color=discord.Color.green(), description=(
        "통화방에 들어가 있는 상태에서 쓰면 그 방으로 같이 할 사람을 구하는 **이미지 글**이 올라가요. "
        "🔊 버튼으로 바로 입장, 참가 버튼으로 참가 신청을 할 수 있어요. 인원이 바뀌면 이미지가 갱신되고, "
        "방이 비거나 시간이 지나면 자동으로 마감돼요."
    ))
    recruit.add_field(name="명령어", inline=False, value="\n".join([
        f"{cmd('구인')} 구인 글 올리기 (게임·티어는 목록에서, 멘션역할로 알림)",
        f"{cmd('구인마감')} 내 구인 글 마감하기",
        f"{cmd('예약')} 시간 정해서 모으기 (10분 전 알림 + 통화방 자동 생성, 꽉 차면 대기)",
        f"{cmd('뽑기')} 항목이나 통화방 사람 중 랜덤 뽑기",
    ]))
    embeds = [room, recruit]
    if is_admin:
        admin = discord.Embed(title="🛠️ 관리자 설정", color=discord.Color.dark_grey(),
                              description="서버 관리 권한이 있는 사람에게만 보여요.")
        admin.add_field(name="생성 채널", inline=False, value="\n".join([
            f"{cmd('통화방설정 만들기')} \"➕ 통화방 만들기\" 채널 만들기",
            f"{cmd('통화방설정 게임채널')} 게임별 생성 채널 만들기 (예: ➕ 발로란트 5인방)",
            f"{cmd('통화방설정 지정')} 기존 음성 채널을 생성 채널로",
            f"{cmd('통화방설정 해제')} 생성 채널 지정 풀기",
            f"{cmd('통화방설정 카테고리')} 방이 만들어질 카테고리",
        ]))
        admin.add_field(name="게임·구인", inline=False, value="\n".join([
            f"{cmd('통화방설정 게임추가')} 게임 등록 (아이콘 첨부 가능)",
            f"{cmd('통화방설정 게임가져오기')} 다른 서버의 게임·아이콘 복사",
            f"{cmd('통화방설정 게임역할')} 게임에 역할 연결 (구인 시 자동 멘션)",
            f"{cmd('통화방설정 게임삭제')} 게임 삭제",
            f"{cmd('통화방설정 구인채널')} 구인 글이 올라갈 채널",
            f"{cmd('통화방설정 목록')} 지금 설정 한눈에 보기",
        ]))
        admin.add_field(name="역할 메뉴 (이모지 → 역할)", inline=False, value="\n".join([
            f"{cmd('역할메뉴 만들기')} 역할 메뉴 글 올리기",
            f"{cmd('역할메뉴 추가')} 이모지와 역할 넣기",
            f"{cmd('역할메뉴 빼기')} 이모지 빼기 (글을 지우면 메뉴도 사라져요)",
        ]))
        admin.add_field(name="추첨 (🎉 반응)", inline=False, value="\n".join([
            f"{cmd('추첨 시작')} 추첨 글 올리기",
            f"{cmd('추첨 끝내기')} 지금 바로 끝내기",
            f"{cmd('추첨 다시뽑기')} 끝난 추첨 다시 뽑기",
        ]))
        admin.set_footer(text="처음이면: 만들기 → 게임추가 → (게임채널·카테고리·구인채널)")
        embeds.append(admin)
    return embeds


@bot.tree.command(name="도움말", description="통화방 봇 명령어를 알려줘요")
@app_commands.guild_only()
async def cmd_help(interaction: discord.Interaction):
    is_admin = interaction.user.guild_permissions.manage_guild
    await interaction.response.send_message(embeds=help_embeds(is_admin), ephemeral=True)


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


# ─────────────────────────────── /예약 ───────────────────────────────
REMIND_BEFORE = 600  # 시작 10분 전에 알림 + 통화방 생성
WEEKDAYS = "월화수목금토일"
TIME_RE = re.compile(
    r"^\s*(?:(?P<day>오늘|내일|모레)|(?P<mon>\d{1,2})\s*(?:[/.\-]|월)\s*(?P<dd>\d{1,2})\s*일?)?\s*"
    r"(?P<ampm>오전|오후|am|pm)?\s*(?P<h>\d{1,2})\s*(?:(?::|시)\s*(?:(?P<m>\d{1,2})\s*분?)?)?\s*(?P<ampm2>am|pm)?\s*$",
    re.IGNORECASE,
)


def parse_time(text: str, now: datetime):
    """'21:00', '오후 9시', '내일 20:30', '10/25 21:00' 같은 글자를 시간으로. 못 읽으면 None."""
    m = TIME_RE.match(text or "")
    if not m:
        return None
    h, mi = int(m["h"]), int(m["m"] or 0)
    ampm = (m["ampm"] or m["ampm2"] or "").lower()
    if ampm in ("오후", "pm") and h < 12:
        h += 12
    if ampm in ("오전", "am") and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    if m["mon"]:
        try:
            t = datetime(now.year, int(m["mon"]), int(m["dd"]), h, mi, tzinfo=TZ)
        except ValueError:
            return None
        if t < now:
            t = t.replace(year=now.year + 1)
        return t
    if m["day"]:
        base = now.date() + timedelta(days={"오늘": 0, "내일": 1, "모레": 2}[m["day"]])
        return datetime.combine(base, dtime(h, mi), TZ)
    t = datetime.combine(now.date(), dtime(h, mi), TZ)
    if t <= now and not ampm and h < 12:  # '9:00'인데 오전 9시가 지났으면 오후 9시로
        t = t.replace(hour=h + 12)
    if t <= now:
        t += timedelta(days=1)
    return t


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


async def res_message(res: dict, message_id: int):
    ch = bot.get_channel(res["channel"])
    if ch is None:
        return None
    try:
        return await ch.fetch_message(message_id)
    except discord.HTTPException:
        return None


async def update_res_post(message_id: int, res: dict, status: str | None = None, msg=None):
    msg = msg or await res_message(res, message_id)
    if msg is None:
        return
    status = status or res_status(res)
    finished = status in ("started", "cancelled")
    try:
        await msg.edit(attachments=[await res_card_file(res, status)], view=res_view(message_id, res, finished))
    except discord.HTTPException as e:
        log.warning("예약 글 갱신 실패 %s: %s", message_id, e)


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
    header = "".join(f"{r.mention} " for r in roles)
    try:
        msg = await target.send(
            content=f"{header}{member.mention} · {time_text(res['start_at'])} 예약",
            file=await res_card_file(res, res_status(res)),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
        await msg.edit(view=res_view(msg.id, res))  # 버튼에 메시지 ID가 들어가야 해서 올린 뒤에 붙임
    except discord.HTTPException:
        await interaction.followup.send(f"{target.mention}에 글을 올리지 못했어요. 봇 권한을 확인해 주세요.", ephemeral=True)
        return
    store.set_res(msg.id, res)
    done = f"예약했어요: {msg.jump_url} ({time_text(res['start_at'])})"
    if roles and not mention_ok(interaction.guild, roles, target):
        done += "\n⚠️ 역할 멘션이 알림으로 가지 않았을 수 있어요. 관리자에게 그 역할의 '@멘션 허용' 설정을 부탁해 주세요."
    await interaction.followup.send(done, ephemeral=True)


# ─────────────────────────────── /뽑기 ───────────────────────────────
@bot.tree.command(name="뽑기", description="항목 중에서, 또는 지금 통화방 사람 중에서 랜덤으로 뽑아요")
@app_commands.guild_only()
@app_commands.describe(항목="쉼표로 구분 (예: 바인드, 헤이븐, 어센트). 비우면 통화방 사람 중에서", 개수="몇 개 뽑을지")
async def cmd_pick(interaction: discord.Interaction, 항목: app_commands.Range[str, 1, 1000] | None = None,
                   개수: app_commands.Range[int, 1, 20] = 1):
    if 항목:
        items = [x.strip() for x in re.split(r"[,\n]", 항목) if x.strip()]
        if len(items) == 1:
            items = 항목.split()
    else:
        voice = interaction.user.voice.channel if interaction.user.voice else None
        if voice is None:
            await interaction.response.send_message("항목을 적거나 통화방에 들어가 있어야 해요.", ephemeral=True)
            return
        items = [m.mention for m in humans(voice)]
    if len(items) < 2:
        await interaction.response.send_message("두 개 이상 있어야 뽑을 수 있어요.", ephemeral=True)
        return
    picked = random.sample(items, min(개수, len(items)))
    await interaction.response.send_message(f"🎲 {'  ·  '.join(picked)}  ({len(items)}개 중)",
                                            allowed_mentions=discord.AllowedMentions.none())


# ─────────────────────────────── /추첨 (이모지 반응 추첨) ───────────────────────────────
GIVEAWAY_EMOJI = "🎉"
DURATION_RE = re.compile(r"(\d+)\s*(일|시간|분|초|d|h|m|s)", re.IGNORECASE)
DURATION_UNITS = {"일": 86400, "d": 86400, "시간": 3600, "h": 3600, "분": 60, "m": 60, "초": 1, "s": 1}


def parse_end(text: str):
    """'10분', '1시간 30분', '2일' 또는 '21:00', '내일 20:00' → 끝나는 시각(타임스탬프)."""
    cleaned = DURATION_RE.sub("", text).strip()
    parts = DURATION_RE.findall(text)
    if parts and not cleaned:
        return time.time() + sum(int(n) * DURATION_UNITS[u.lower()] for n, u in parts)
    t = parse_time(text, datetime.now(TZ))
    return t.timestamp() if t else None


def giveaway_embed(g: dict, winners: list[int] | None = None, entrants: int | None = None) -> discord.Embed:
    end = int(g["end_at"])
    if winners is None:
        desc = f"당첨 {g['count']}명\n<t:{end}:R> 마감 (<t:{end}:t>)\n주최 <@{g['host']}>"
        color = discord.Color.gold()
    else:
        desc = (f"당첨 {' '.join(f'<@{w}>' for w in winners) or '없음'}\n"
                f"참여 {entrants}명\n주최 <@{g['host']}>")
        color = discord.Color.dark_grey()
    return discord.Embed(title=f"{GIVEAWAY_EMOJI} {g['prize']}", description=desc, color=color)


async def giveaway_message(g: dict, message_id: int):
    ch = bot.get_channel(g["channel"])
    if ch is None:
        return None
    try:
        return await ch.fetch_message(message_id)
    except discord.HTTPException:
        return None


async def giveaway_entrants(msg: discord.Message) -> list:
    reaction = discord.utils.get(msg.reactions, emoji=GIVEAWAY_EMOJI)
    if reaction is None:
        return []
    return [u async for u in reaction.users(limit=None) if not u.bot]


async def finish_giveaway(message_id: int, reroll: bool = False):
    g = store.data["giveaways"].get(str(message_id))
    if not g:
        return None
    msg = await giveaway_message(g, message_id)
    if msg is None:
        store.data["giveaways"].pop(str(message_id), None)
        store.save()
        return None
    entrants = await giveaway_entrants(msg)
    previous = set(g.get("winners", [])) if reroll else set()
    pool = [u for u in entrants if u.id not in previous]
    winners = random.sample(pool, min(g["count"], len(pool))) if pool else []
    g.update(ended=True, winners=[w.id for w in winners], ended_at=time.time())
    store.save()
    try:
        await msg.edit(embed=giveaway_embed(g, g["winners"], len(entrants)))
        if winners:
            label = "다시 뽑기" if reroll else "당첨"
            await msg.reply(f"{GIVEAWAY_EMOJI} {' '.join(w.mention for w in winners)} {label} · {g['prize']}",
                            allowed_mentions=discord.AllowedMentions(users=True))
        else:
            await msg.reply("참여한 사람이 없어서 당첨자가 없어요.")
    except discord.HTTPException as e:
        log.warning("추첨 결과 전송 실패: %s", e)
    return winners


@tasks.loop(seconds=15)
async def giveaway_tick():
    now = time.time()
    for mid, g in list(store.data["giveaways"].items()):
        try:
            if not g.get("ended") and now >= g["end_at"]:
                await finish_giveaway(int(mid))
            elif g.get("ended") and now - g.get("ended_at", now) > 7 * 86400:  # 끝난 지 7일 지나면 정리
                store.data["giveaways"].pop(mid, None)
                store.save()
        except Exception:
            log.exception("추첨 처리 실패 %s", mid)


def giveaway_choices(interaction: discord.Interaction, current: str, ended: bool):
    out = []
    for mid, g in store.data["giveaways"].items():
        if g["guild"] == interaction.guild_id and bool(g.get("ended")) == ended and current.lower() in g["prize"].lower():
            out.append(app_commands.Choice(name=g["prize"][:100], value=mid))
    return out[:25]


async def active_giveaway_autocomplete(interaction: discord.Interaction, current: str):
    return giveaway_choices(interaction, current, ended=False)


async def ended_giveaway_autocomplete(interaction: discord.Interaction, current: str):
    return giveaway_choices(interaction, current, ended=True)


giveaway_group = app_commands.Group(
    name="추첨", description="🎉 반응을 누른 사람 중에서 추첨해요", guild_only=True,
    default_permissions=discord.Permissions(manage_messages=True),
)


@giveaway_group.command(name="시작", description="추첨 글을 올려요")
@app_commands.describe(상품="예: 문화상품권 5천원", 시간="예: 10분, 1시간 30분, 2일, 21:00", 당첨="당첨자 수",
                       채널="올릴 채널 (비우면 지금 채널)")
async def cmd_giveaway_start(interaction: discord.Interaction, 상품: app_commands.Range[str, 1, 200],
                             시간: app_commands.Range[str, 1, 30], 당첨: app_commands.Range[int, 1, 20] = 1,
                             채널: discord.TextChannel | None = None):
    end_at = parse_end(시간)
    if end_at is None or end_at - time.time() < 10:
        await interaction.response.send_message("시간을 읽지 못했어요. `10분`, `1시간 30분`, `21:00`처럼 적어 주세요.",
                                                ephemeral=True)
        return
    if end_at - time.time() > 30 * 86400:
        await interaction.response.send_message("추첨은 30일 안쪽으로만 할 수 있어요.", ephemeral=True)
        return
    target = 채널 or interaction.channel
    g = {"guild": interaction.guild.id, "channel": target.id, "prize": 상품, "count": 당첨,
         "end_at": end_at, "host": interaction.user.id, "ended": False}
    try:
        msg = await target.send(embed=giveaway_embed(g))
        await msg.add_reaction(GIVEAWAY_EMOJI)
    except discord.HTTPException:
        await interaction.response.send_message(f"{target.mention}에 글을 올리지 못했어요. 봇 권한을 확인해 주세요.",
                                                ephemeral=True)
        return
    store.data["giveaways"][str(msg.id)] = g
    store.save()
    await interaction.response.send_message(f"추첨을 올렸어요: {msg.jump_url}", ephemeral=True)


@giveaway_group.command(name="끝내기", description="진행 중인 추첨을 지금 바로 끝내고 당첨자를 뽑아요")
@app_commands.autocomplete(추첨=active_giveaway_autocomplete)
async def cmd_giveaway_end(interaction: discord.Interaction, 추첨: str):
    g = store.data["giveaways"].get(추첨)
    if not g or g.get("ended"):
        await interaction.response.send_message("진행 중인 추첨을 목록에서 골라 주세요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await finish_giveaway(int(추첨))
    await interaction.followup.send("끝냈어요.", ephemeral=True)


@giveaway_group.command(name="다시뽑기", description="끝난 추첨에서 당첨자를 다시 뽑아요 (이전 당첨자 제외)")
@app_commands.autocomplete(추첨=ended_giveaway_autocomplete)
async def cmd_giveaway_reroll(interaction: discord.Interaction, 추첨: str):
    g = store.data["giveaways"].get(추첨)
    if not g or not g.get("ended"):
        await interaction.response.send_message("끝난 추첨을 목록에서 골라 주세요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    winners = await finish_giveaway(int(추첨), reroll=True)
    await interaction.followup.send("다시 뽑았어요." if winners else "뽑을 사람이 없어요.", ephemeral=True)


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
bot.tree.add_command(menu_group)
bot.tree.add_command(giveaway_group)

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN 환경변수가 없어요. .env 파일을 확인해 주세요.")
    bot.run(TOKEN, log_handler=None)
