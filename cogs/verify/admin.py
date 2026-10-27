"""/인증 (관리자): 역할·채널 설정, 인증 채널 만들기, 안내 패널, 설정 보기"""

from discord import app_commands
import discord

from core.client import bot
from cogs.verify import data
from cogs.verify.views import send_panel


group = app_commands.Group(
    name="인증", description="입장 인증 설정 (관리자)", guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


def info_embed(guild: discord.Guild) -> discord.Embed:
    c = data.cfg(guild.id)

    def role(key):
        rid = c.get(key)
        return "❌ 안 정해짐" if not rid else (f"<@&{rid}>" if guild.get_role(rid) else "⚠️ 역할을 찾을 수 없어요")

    def chan(key, optional=False):
        cid = c.get(key)
        if not cid:
            return "없음" if optional else "❌ 안 정해짐"
        return f"<#{cid}>" if guild.get_channel(cid) else "⚠️ 채널을 찾을 수 없어요"

    left = data.missing(guild.id)
    embed = discord.Embed(title="입장 인증 설정", color=discord.Color.green() if not left else discord.Color.orange(),
                          description="✅ 설정이 다 끝났어요!" if not left else f"⚠️ 아직 필요해요: {', '.join(left)}")
    embed.add_field(name="정상인 역할 (승인되면)", value=role("member_role"))
    embed.add_field(name="관리자 역할 (신청 멘션)", value=role("admin_role"))
    embed.add_field(name="남성 / 여성 역할", value=f"{role('male_role')} / {role('female_role')}")
    embed.add_field(name="인증 채널", value=chan("channel"))
    embed.add_field(name="승인 알림 채널", value=chan("notify"))
    embed.add_field(name="게임 역할 채널 (승인 뒤 안내)", value=chan("game_channel", optional=True))
    if c.get("panel") and c.get("panel_channel"):
        embed.add_field(name="안내 패널", value=f"[바로가기](https://discord.com/channels/{guild.id}/{c['panel_channel']}/{c['panel']})")
    embed.set_footer(text=f"승인 대기 중인 신청 {data.count_pending(guild.id)}건")
    return embed


async def delete_old_panel(guild: discord.Guild):
    c = data.cfg(guild.id)
    ch = guild.get_channel(c.get("panel_channel") or 0)
    if ch and c.get("panel"):
        try:
            await ch.get_partial_message(c["panel"]).delete()
        except discord.HTTPException:
            pass


async def post_new_panel(channel: discord.TextChannel) -> discord.Message:
    """옛 패널을 지우고 새 패널을 올려서 기억해 둠."""
    await delete_old_panel(channel.guild)
    msg = await send_panel(channel)
    c = data.cfg(channel.guild.id)
    c["channel"], c["panel"], c["panel_channel"] = channel.id, msg.id, channel.id
    data.save()
    return msg


@group.command(name="설정", description="인증에 쓸 역할·채널을 정하거나 바꿔요 (바꿀 것만 입력하면 돼요)")
@app_commands.describe(정상인역할="승인되면 받는 역할", 관리자역할="신청이 오면 멘션할 관리자 역할",
                       남성역할="성별 '남성' 역할 (선택하면 바로 부여)", 여성역할="성별 '여성' 역할 (선택하면 바로 부여)",
                       승인알림채널="신청 글이 올라올 관리자 채널", 게임역할채널="승인 뒤 안내할 게임 역할 고르는 채널")
async def cmd_config(interaction: discord.Interaction,
                     정상인역할: discord.Role | None = None, 관리자역할: discord.Role | None = None,
                     남성역할: discord.Role | None = None, 여성역할: discord.Role | None = None,
                     승인알림채널: discord.TextChannel | None = None, 게임역할채널: discord.TextChannel | None = None):
    guild = interaction.guild
    c = data.cfg(guild.id)
    warns = []
    picks = {"member_role": 정상인역할, "admin_role": 관리자역할, "male_role": 남성역할, "female_role": 여성역할,
             "notify": 승인알림채널, "game_channel": 게임역할채널}
    for key, value in picks.items():
        if value is None:
            continue
        if isinstance(value, discord.Role):
            if value.is_default():
                warns.append("⚠️ @everyone은 쓸 수 없어요.")
                continue
            if key != "admin_role" and (value.managed or value >= guild.me.top_role):
                warns.append(f"⚠️ {value.mention}은(는) 봇보다 위에 있거나 봇 전용이라 줄 수 없어요. "
                             "서버 설정 → 역할에서 봇 역할을 더 위로 올려 주세요.")
        c[key] = value.id
    data.save()
    await interaction.response.send_message("\n".join(warns) or None, embed=info_embed(guild), ephemeral=True)


@group.command(name="채널만들기", description="인증 채널을 새로 만들고 안내 패널을 올려요 ('정상인' 역할은 못 보게 설정)")
@app_commands.describe(이름="채널 이름", 카테고리="채널을 만들 카테고리 (비우면 맨 위)")
async def cmd_make_channel(interaction: discord.Interaction, 이름: app_commands.Range[str, 1, 100] = "인증",
                           카테고리: discord.CategoryChannel | None = None):
    guild = interaction.guild
    member_role = guild.get_role(data.cfg(guild.id).get("member_role") or 0)
    if member_role is None:
        await interaction.response.send_message("먼저 `/인증 설정`으로 **정상인역할**을 정해 주세요!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    overwrites = {
        # 모두 볼 수 있지만 글은 못 씀 (버튼만 누르면 돼요)
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True, read_message_history=True, send_messages=False, add_reactions=False,
            create_public_threads=False, create_private_threads=False, send_messages_in_threads=False),
        # 정상인 역할은 안 보임 → 승인되면 인증 채널이 사라져요
        member_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                              read_message_history=True),
    }
    try:
        ch = await guild.create_text_channel(이름, category=카테고리, overwrites=overwrites,
                                             topic="아래 버튼을 눌러 인증해 주세요", reason="인증 채널 만들기")
        await post_new_panel(ch)
    except discord.HTTPException:
        await interaction.followup.send("채널을 못 만들었어요! 봇에게 '채널 관리' 권한이 있는지 확인해 주세요.", ephemeral=True)
        return
    left = data.missing(guild.id)
    note = f"\n⚠️ 아직 필요한 설정: {', '.join(left)} → `/인증 설정`" if left else ""
    await interaction.followup.send(f"{ch.mention} 만들고 안내 패널도 올렸어요!{note}", ephemeral=True)


@group.command(name="패널", description="인증 안내 패널을 (다시) 올려요. 문구도 바꿀 수 있어요")
@app_commands.describe(채널="올릴 채널 (비우면 지금 인증 채널)", 제목="패널 제목", 설명="패널 설명 (줄바꿈은 \\n)")
async def cmd_panel(interaction: discord.Interaction, 채널: discord.TextChannel | None = None,
                    제목: app_commands.Range[str, 1, 100] | None = None,
                    설명: app_commands.Range[str, 1, 1000] | None = None):
    guild = interaction.guild
    c = data.cfg(guild.id)
    target = 채널 or guild.get_channel(c.get("channel") or 0)
    if not isinstance(target, discord.TextChannel):
        await interaction.response.send_message("올릴 채널이 없어요! `/인증 채널만들기`를 쓰거나 채널을 골라 주세요.",
                                                ephemeral=True)
        return
    if 제목:
        c["title"] = 제목
    if 설명:
        c["desc"] = 설명.replace("\\n", "\n")
    await interaction.response.defer(ephemeral=True)
    try:
        await post_new_panel(target)
    except discord.HTTPException:
        await interaction.followup.send(f"{target.mention}에 글을 못 올렸어요! 봇의 '채널 보기'·'메시지 보내기' 권한을 확인해 주세요.",
                                        ephemeral=True)
        return
    msg = f"{target.mention}에 패널을 올렸어요!"
    member_role = guild.get_role(c.get("member_role") or 0)
    if member_role and target.overwrites_for(member_role).view_channel is not False:
        msg += (f"\n⚠️ 이 채널은 {member_role.mention}도 볼 수 있어요. 채널 권한에서 그 역할의 '채널 보기'를 꺼야 "
                "승인된 뒤 인증 채널이 사라져요.")
    await interaction.followup.send(msg, ephemeral=True)


@group.command(name="정보", description="지금 인증 설정을 보여줘요")
async def cmd_info(interaction: discord.Interaction):
    await interaction.response.send_message(embed=info_embed(interaction.guild), ephemeral=True)


bot.tree.add_command(group)
