"""/역할메뉴, /자동역할, 이모지 눌러 역할 받기, 들어올 때 자동 역할"""

from discord import app_commands
import discord

from core.config import log
from core.store import game_key, store
from core.client import bot
from core.utils import admin_log


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return
    roles = [r for r in (member.guild.get_role(rid) for rid in store.guild(member.guild.id).get("auto_roles", []))
             if r and r < member.guild.me.top_role]
    if roles:
        try:
            await member.add_roles(*roles, reason="자동 역할")
            await admin_log(member.guild, f"🏷️ 자동 역할 · {member.mention} · {' '.join(r.mention for r in roles)}")
        except discord.HTTPException as e:
            log.warning("자동 역할 실패 (%s): %s", member.guild.name, e)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    await apply_menu_reaction(payload, add=True)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    await apply_menu_reaction(payload, add=False)


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
            await admin_log(guild, f"🏷️ 역할 받음 · {member.mention} · {role.mention}")
        elif not add and role in member.roles:
            await member.remove_roles(role, reason="역할 메뉴")
            await admin_log(guild, f"🏷️ 역할 뺌 · {member.mention} · {role.mention}")
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


# ─────────────────────────────── /자동역할 ───────────────────────────────
auto_role_group = app_commands.Group(
    name="자동역할", description="서버에 들어온 사람에게 자동으로 주는 역할 (관리자)", guild_only=True,
    default_permissions=discord.Permissions(manage_roles=True),
)
MEMBERS_INTENT_HELP = ("자동 역할을 쓰려면 먼저 설정이 필요해요.\n"
                       "1. 디스코드 개발자 포털 → 봇 → Bot → **Server Members Intent** 켜기\n"
                       "2. VM의 `.env`에 `MEMBERS_INTENT=1` 한 줄 추가 후 봇 재시작")


def auto_roles(guild_id: int) -> list:
    return store.guild(guild_id).setdefault("auto_roles", [])


@auto_role_group.command(name="목록", description="자동 역할 목록을 봐요")
async def cmd_auto_role_list(interaction: discord.Interaction):
    roles = auto_roles(interaction.guild.id)
    text = "\n".join(f"<@&{r}>" for r in roles) or "없음"
    if not bot.intents.members:
        text += "\n\n⚠️ " + MEMBERS_INTENT_HELP
    await interaction.response.send_message(embed=discord.Embed(title="자동 역할", description=text,
                                                                color=discord.Color.blurple()), ephemeral=True)


@auto_role_group.command(name="추가", description="자동 역할을 추가해요")
async def cmd_auto_role_add(interaction: discord.Interaction, 역할: discord.Role):
    if not bot.intents.members:
        await interaction.response.send_message(MEMBERS_INTENT_HELP, ephemeral=True)
        return
    problem = role_problem(interaction.guild, 역할)
    if problem:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    roles = auto_roles(interaction.guild.id)
    if 역할.id in roles:
        await interaction.response.send_message(f"{역할.mention}은(는) 이미 자동 역할이에요.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())
        return
    roles.append(역할.id)
    store.save()
    await interaction.response.send_message(f"{역할.mention} 추가했어요.", ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())


async def auto_role_autocomplete(interaction: discord.Interaction, current: str):
    out = []
    for rid in auto_roles(interaction.guild_id):
        role = interaction.guild.get_role(rid)
        name = role.name if role else f"(삭제된 역할 {rid})"
        if current.lower() in name.lower():
            out.append(app_commands.Choice(name=name[:100], value=str(rid)))
    return out[:25]


@auto_role_group.command(name="빼기", description="자동 역할을 빼요")
@app_commands.autocomplete(역할=auto_role_autocomplete)
async def cmd_auto_role_remove(interaction: discord.Interaction, 역할: str):
    roles = auto_roles(interaction.guild.id)
    rid = int(역할) if 역할.isdigit() else None
    if rid not in roles:
        await interaction.response.send_message("목록에서 골라 주세요.", ephemeral=True)
        return
    roles.remove(rid)
    store.save()
    await interaction.response.send_message(f"<@&{rid}> 뺐어요.", ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())
bot.tree.add_command(menu_group)
bot.tree.add_command(auto_role_group)
