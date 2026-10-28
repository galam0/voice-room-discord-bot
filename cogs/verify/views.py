"""입장 인증: 안내 패널 버튼, 닉네임·출생년도 입력 팝업, 성별 선택, 관리자 승인/거절

흐름
  패널의 [인증 시작] → 팝업(닉네임·태어난 연도) → 닉네임이 '닉네임 /07'로 바로 바뀜
  → 비공개 성별 드롭다운 선택 → 성별 역할이 바로 부여됨 + 관리자 채널에 신청 글(승인/거절 버튼)
  → 관리자가 [승인] → '정상인' 역할 부여 + 게임 역할 채널에 안내
버튼은 custom_id로 처리해서 봇이 재시작돼도 동작해요 (기존 구인·예약 버튼과 같은 방식).
"""

from datetime import datetime

import discord

from core.client import bot
from core.config import TZ, log
from core.utils import admin_log
from cogs.verify import data


# 닉네임 뒤에 ' /07'(4글자)이 붙고, 디스코드 서버 닉네임은 32자까지라서
NICK_MAX = 32 - len(" /07")

GENDERS = {"m": ("남성", "male_role"), "f": ("여성", "female_role")}

DEFAULT_TITLE = "서버 인증"
DEFAULT_DESC = (
    "아래 **인증 시작** 버튼을 눌러 닉네임과 태어난 연도를 입력한 뒤, 다음 화면에서 성별을 선택해 주세요.\n\n"
    "• 닉네임은 `닉네임 /출생년도 두 자리`로 바뀌어요 (예: `철수 /07`)\n"
    "• 태어난 연도는 네 자리 숫자로 입력해 주세요 (예: 2007 → /07, 2010 → /10)\n"
    "• 관리자가 확인하면 입장이 완료돼요"
)


# ─────────────────────────────── 도우미 ───────────────────────────────
def full_nick(nick: str, birth: int) -> str:
    return f"{nick} /{birth % 100:02d}"


def is_verify_admin(member: discord.Member) -> bool:
    rid = data.cfg(member.guild.id).get("admin_role")
    return member.guild_permissions.manage_guild or bool(rid and any(r.id == rid for r in member.roles))


def panel_embed(guild_id: int) -> discord.Embed:
    c = data.cfg(guild_id)
    return discord.Embed(title=c.get("title") or DEFAULT_TITLE, description=c.get("desc") or DEFAULT_DESC,
                         color=discord.Color.green())


def panel_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="인증 시작", emoji="✅", style=discord.ButtonStyle.success,
                                    custom_id="verify_start"))
    return view


class GenderSelect(discord.ui.Select):
    """인증 진행 중인 사용자에게만 보이는 성별 드롭다운."""

    def __init__(self):
        options = [
            discord.SelectOption(label=label, value=code, description=f"{label} 선택")
            for code, (label, _) in GENDERS.items()
        ]
        super().__init__(
            placeholder="성별을 선택해 주세요",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="verify_gender_select",
        )

    async def callback(self, interaction: discord.Interaction):
        await handle_gender(interaction, self.values[0])


def gender_view() -> discord.ui.View:
    view = discord.ui.View(timeout=300)
    view.add_item(GenderSelect())
    return view


def request_view(user_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="승인", emoji="✅", style=discord.ButtonStyle.success,
                                    custom_id=f"verify_ok:{user_id}"))
    view.add_item(discord.ui.Button(label="거절", emoji="❌", style=discord.ButtonStyle.danger,
                                    custom_id=f"verify_no:{user_id}"))
    return view


def request_embed(member: discord.Member, info: dict) -> discord.Embed:
    gender = GENDERS.get(info.get("gender"), ("선택 안 함",))[0]
    embed = discord.Embed(title="인증 신청", color=discord.Color.orange())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="신청자", value=member.mention, inline=False)
    embed.add_field(name="바뀐 닉네임", value=full_nick(info["nick"], info["birth"]))
    embed.add_field(name="출생년도", value=f"{info['birth']}년생")
    embed.add_field(name="성별", value=gender)
    embed.set_footer(text="승인하면 '정상인' 역할이 부여돼요")
    return embed


async def dm_member(member: discord.Member, text: str) -> bool:
    """본인에게만 보이는 DM 보내기. DM을 막아 둔 사람이면 False."""
    try:
        await member.send(text)
        return True
    except discord.HTTPException:  # Forbidden 포함
        return False


async def send_panel(channel: discord.TextChannel) -> discord.Message:
    return await channel.send(embed=panel_embed(channel.guild.id), view=panel_view())


async def post_request(guild: discord.Guild, member: discord.Member, info: dict) -> bool:
    """관리자 채널에 신청 글 올리기 (이미 올린 글이 있으면 고치기만 해서 알림이 또 가지 않게)."""
    c = data.cfg(guild.id)
    ch = guild.get_channel(c.get("notify") or 0)
    if ch is None:
        return False
    embed, view = request_embed(member, info), request_view(member.id)
    if info.get("msg"):
        try:
            msg = await ch.fetch_message(info["msg"])
            await msg.edit(embed=embed, view=view)
            return True
        except discord.HTTPException:
            pass  # 글이 지워졌으면 아래에서 새로 올림
    admin_role = guild.get_role(c.get("admin_role") or 0)
    mentions = discord.AllowedMentions(roles=[admin_role]) if admin_role else discord.AllowedMentions.none()
    msg = await ch.send(f"{admin_role.mention if admin_role else ''} 새 인증 신청이 왔어요!".strip(),
                        embed=embed, view=view, allowed_mentions=mentions)
    info["msg"], info["channel"] = msg.id, ch.id
    data.set_pending(guild.id, member.id, info)
    return True


# ─────────────────────────────── 입력 팝업 ───────────────────────────────
class VerifyModal(discord.ui.Modal, title="서버 인증"):
    def __init__(self):
        super().__init__()
        self.nick = discord.ui.TextInput(label="닉네임", placeholder="서버에서 쓸 이름", min_length=1, max_length=NICK_MAX)
        self.birth = discord.ui.TextInput(label="태어난 연도 (4자리 숫자)", placeholder="예: 2007", min_length=4, max_length=4)
        self.add_item(self.nick)
        self.add_item(self.birth)

    async def on_submit(self, interaction: discord.Interaction):
        nick = " ".join(self.nick.value.split())
        birth_text = self.birth.value.strip()
        current_year = datetime.now(TZ).year
        if not nick or not birth_text.isdecimal() or len(birth_text) != 4 or not 1900 <= int(birth_text) <= current_year:
            await interaction.response.send_message(
                f"닉네임을 적고, 태어난 연도는 1900~{current_year}년 사이의 네 자리 숫자로 입력해 주세요!",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        guild, member = interaction.guild, interaction.user
        birth = int(birth_text)
        shown = full_nick(nick, birth)

        text = f"닉네임이 **{shown}**(으)로 바뀌었어요!\n아래 드롭다운에서 성별을 선택해 주세요 👇"
        try:
            await member.edit(nick=shown, reason="인증 신청")
        except discord.HTTPException:
            text = ("⚠️ 봇이 닉네임을 못 바꿨어요 (서버 주인이거나 봇보다 높은 역할이면 바꿀 수 없어요). "
                    "관리자가 직접 바꿔 줄 거예요.\n아래 드롭다운에서 성별을 선택해 주세요 👇")

        old = data.pending(guild.id, member.id) or {}
        data.set_pending(guild.id, member.id, {"nick": nick, "birth": birth, "gender": None,
                                               "msg": old.get("msg"), "channel": old.get("channel")})
        await interaction.followup.send(text, view=gender_view(), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.exception("인증 팝업 오류", exc_info=error)
        msg = "실패했어요! 봇 권한을 확인해 보세요."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


# ─────────────────────────────── 버튼 처리 ───────────────────────────────
async def handle_start(interaction: discord.Interaction):
    guild, member = interaction.guild, interaction.user
    if guild is None:
        return
    if left := data.missing(guild.id):
        await interaction.response.send_message(
            f"인증 설정이 아직 덜 끝났어요 ({', '.join(left)}). 관리자에게 알려 주세요!", ephemeral=True)
        return
    c = data.cfg(guild.id)
    if any(r.id == c["member_role"] for r in member.roles):
        await interaction.response.send_message("이미 인증된 멤버예요!", ephemeral=True)
        return
    info = data.pending(guild.id, member.id)
    if info and info.get("msg"):
        await interaction.response.send_message(
            "이미 신청이 접수됐어요. 관리자가 확인할 때까지 기다려 주세요 🙌", ephemeral=True)
        return
    await interaction.response.send_modal(VerifyModal())


async def handle_gender(interaction: discord.Interaction, code: str):
    guild, member = interaction.guild, interaction.user
    info = data.pending(guild.id, member.id)
    if info is None:
        await interaction.response.send_message("신청 정보가 없어요. 인증 채널에서 '인증 시작'부터 다시 눌러 주세요!",
                                                ephemeral=True)
        return
    c = data.cfg(guild.id)
    label, key = GENDERS[code]
    role = guild.get_role(c.get(key) or 0)
    if role is None:
        await interaction.response.send_message("성별 역할이 설정되지 않았어요. 관리자에게 알려 주세요!", ephemeral=True)
        return
    await interaction.response.defer()
    other = guild.get_role(c.get("female_role" if code == "m" else "male_role") or 0)
    try:
        await member.add_roles(role, reason="인증: 성별 선택")
        if other and other in member.roles:
            await member.remove_roles(other, reason="인증: 성별 변경")
    except discord.HTTPException:
        await interaction.edit_original_response(
            content="역할을 줄 수 없어요! 관리자에게 봇 역할이 성별 역할보다 위에 있는지 확인해 달라고 해 주세요.")
        return
    info["gender"] = code
    data.set_pending(guild.id, member.id, info)
    if not await post_request(guild, member, info):
        await interaction.edit_original_response(content="관리자 알림 채널이 설정되지 않았어요. 관리자에게 알려 주세요!")
        return
    await interaction.edit_original_response(
        content=f"✅ 신청 완료! (성별: {label})\n관리자가 확인하면 입장이 끝나요. 잠시만 기다려 주세요 🙌", view=None)


async def handle_decision(interaction: discord.Interaction, user_id: int, approve: bool):
    guild = interaction.guild
    if guild is None:
        return
    if not is_verify_admin(interaction.user):
        await interaction.response.send_message("관리자만 누를 수 있어요!", ephemeral=True)
        return
    info = data.pending(guild.id, user_id)
    if info is None:  # 이미 처리됐거나 기록이 사라진 신청 → 버튼만 치움
        await interaction.response.edit_message(content="이미 처리된 신청이에요.", view=None)
        return
    c = data.cfg(guild.id)
    embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed(title="인증 신청")
    try:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
    except discord.NotFound:
        data.remove_pending(guild.id, user_id)
        embed.color = discord.Color.light_grey()
        await interaction.response.edit_message(content="⚪ 서버를 나간 사용자예요.", embed=embed, view=None)
        return

    if approve:
        role = guild.get_role(c.get("member_role") or 0)
        if role is None:
            await interaction.response.send_message("'정상인' 역할이 설정되지 않았어요! `/인증 설정`으로 정해 주세요.",
                                                    ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await member.add_roles(role, reason=f"인증 승인 ({interaction.user})")
        except discord.HTTPException:
            await interaction.followup.send("역할을 줄 수 없어요! 봇 역할이 '정상인' 역할보다 위에 있는지 확인해 주세요.",
                                            ephemeral=True)
            return
        data.remove_pending(guild.id, user_id)
        embed.color = discord.Color.green()
        embed.set_footer(text=f"✅ {interaction.user.display_name} 님이 승인")
        await interaction.edit_original_response(content="✅ 승인 완료", embed=embed, view=None)
        game_ch = guild.get_channel(c.get("game_channel") or 0)
        text = f"✅ **{guild.name}** 인증이 완료됐어요! 🎉"
        if game_ch:
            text += f"\n{game_ch.mention} 에서 원하는 게임 역할을 골라 주세요 🎮"
        if not await dm_member(member, text):
            await interaction.followup.send(
                f"승인은 됐지만 {member.mention} 님이 DM을 막아뒀어요. 직접 알려 주세요!",
                ephemeral=True)
        await admin_log(guild, f"✅ 인증 승인 · {member.mention} · {interaction.user.mention}")
        return

    await interaction.response.defer()
    for key in ("male_role", "female_role"):  # 다시 신청할 때 새로 고르게 성별 역할을 거둠
        role = guild.get_role(c.get(key) or 0)
        if role and role in member.roles:
            try:
                await member.remove_roles(role, reason="인증 거절")
            except discord.HTTPException:
                pass
    data.remove_pending(guild.id, user_id)
    embed.color = discord.Color.red()
    embed.set_footer(text=f"❌ {interaction.user.display_name} 님이 거절")
    await interaction.edit_original_response(content="❌ 거절됨", embed=embed, view=None)
    verify_ch = guild.get_channel(c.get("channel") or 0)
    text = f"❌ **{guild.name}** 인증이 거절됐어요. 입력한 정보를 확인하고 다시 신청해 주세요."
    if verify_ch:
        text += f"\n{verify_ch.mention} 에서 '인증 시작'을 다시 누르면 돼요."
    if not await dm_member(member, text):
        await interaction.followup.send(
            f"거절은 됐지만 {member.mention} 님이 DM을 막아뒀어요. 직접 알려 주세요!", ephemeral=True)
    await admin_log(guild, f"❌ 인증 거절 · {member.mention} · {interaction.user.mention}")


# ─────────────────────────────── 이벤트 ───────────────────────────────
@bot.listen("on_interaction")
async def on_verify_interaction(interaction: discord.Interaction):
    """인증 버튼 (봇이 재시작돼도 동작하도록 custom_id로 처리). 다른 기능의 버튼은 건드리지 않아요."""
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = (interaction.data or {}).get("custom_id", "")
    if not custom_id.startswith("verify_"):
        return
    try:
        if custom_id == "verify_start":
            await handle_start(interaction)
        elif custom_id.startswith("verify_gender:"):
            await handle_gender(interaction, custom_id.split(":")[1])
        elif custom_id.startswith("verify_ok:"):
            await handle_decision(interaction, int(custom_id.split(":")[1]), approve=True)
        elif custom_id.startswith("verify_no:"):
            await handle_decision(interaction, int(custom_id.split(":")[1]), approve=False)
    except discord.HTTPException as e:
        log.warning("인증 버튼 처리 실패 %s: %s", custom_id, e)
        if not interaction.response.is_done():
            await interaction.response.send_message("실패했어요! 봇 권한을 확인해 보세요.", ephemeral=True)


@bot.listen("on_raw_message_delete")
async def on_verify_message_delete(payload: discord.RawMessageDeleteEvent):
    data.remove_pending_by_message(payload.message_id)
