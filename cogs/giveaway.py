"""/뽑기, /추첨"""

from datetime import datetime
import random
import re
import time

from discord import app_commands
from discord.ext import tasks
import discord

from core.config import TZ, log
from core.store import store
from core.client import bot
from core.utils import admin_log, humans
from core.timeparse import parse_time


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
            await interaction.response.send_message("항목을 적거나 통화방에 들어가 있어야 돼요!", ephemeral=True)
            return
        items = [m.mention for m in humans(voice)]
    if len(items) < 2:
        msg = "두 개 이상 적어야 뽑을 수 있어요!" if 항목 else "통화방에 2명 이상 있어야 뽑을 수 있어요!"
        await interaction.response.send_message(msg, ephemeral=True)
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
    if reroll:  # 지금까지 당첨된 사람은 모두 빼고
        g["past_winners"] = sorted(set(g.get("past_winners", [])) | set(g.get("winners", [])))
    previous = set(g.get("past_winners", [])) if reroll else set()
    pool = [u for u in entrants if u.id not in previous]
    winners = random.sample(pool, min(g["count"], len(pool))) if pool else []
    g.update(ended=True, winners=[w.id for w in winners], ended_at=time.time())
    await admin_log(msg.guild, f"🎉 추첨 {'다시 뽑기' if reroll else '결과'} · {g['prize']} · "
                               f"{' '.join(w.mention for w in winners) or '당첨자 없음'} ({len(entrants)}명 참여)")
    store.save()
    try:
        await msg.edit(embed=giveaway_embed(g, g["winners"], len(entrants)))
        if winners:
            label = "다시 뽑기" if reroll else "당첨"
            await msg.reply(f"{GIVEAWAY_EMOJI} {' '.join(w.mention for w in winners)} {label} · {g['prize']}",
                            allowed_mentions=discord.AllowedMentions(users=True))
        else:
            await msg.reply("참여한 사람이 없어서 당첨자 없음")
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
    if end_at is not None and end_at <= time.time():
        await interaction.response.send_message("이미 지난 시간이에요!", ephemeral=True)
        return
    if end_at is None or end_at - time.time() < 10:
        await interaction.response.send_message("시간을 못 알아들었어요! `10분`, `1시간 30분`, `21:00`처럼 적어 주세요.",
                                                ephemeral=True)
        return
    if end_at - time.time() > 30 * 86400:
        await interaction.response.send_message("추첨은 최대 30일까지만 돼요!", ephemeral=True)
        return
    target = 채널 or interaction.channel
    g = {"guild": interaction.guild.id, "channel": target.id, "prize": 상품, "count": 당첨,
         "end_at": end_at, "host": interaction.user.id, "ended": False}
    await interaction.response.defer(ephemeral=True)
    try:
        msg = await target.send(embed=giveaway_embed(g))
        await msg.add_reaction(GIVEAWAY_EMOJI)
    except discord.HTTPException:
        await interaction.followup.send(f"{target.mention}에 글을 못 올렸어요! 봇 권한을 확인해 보세요.", ephemeral=True)
        return
    store.data["giveaways"][str(msg.id)] = g
    store.save()
    await interaction.followup.send(f"추첨 올렸어요! {msg.jump_url}", ephemeral=True)


@giveaway_group.command(name="끝내기", description="진행 중인 추첨을 지금 바로 끝내고 당첨자를 뽑아요")
@app_commands.autocomplete(추첨=active_giveaway_autocomplete)
async def cmd_giveaway_end(interaction: discord.Interaction, 추첨: str):
    g = store.data["giveaways"].get(추첨)
    if not g or g.get("ended"):
        await interaction.response.send_message("목록에서 진행 중인 추첨을 골라 주세요!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await finish_giveaway(int(추첨))
    await interaction.followup.send("추첨 끝!", ephemeral=True)


@giveaway_group.command(name="다시뽑기", description="끝난 추첨에서 당첨자를 다시 뽑아요 (지금까지 당첨된 사람 제외)")
@app_commands.autocomplete(추첨=ended_giveaway_autocomplete)
async def cmd_giveaway_reroll(interaction: discord.Interaction, 추첨: str):
    g = store.data["giveaways"].get(추첨)
    if not g or not g.get("ended"):
        await interaction.response.send_message("목록에서 끝난 추첨을 골라 주세요!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    winners = await finish_giveaway(int(추첨), reroll=True)
    await interaction.followup.send("다시 뽑았어요!" if winners else "뽑을 사람이 없어요!", ephemeral=True)


bot.tree.add_command(giveaway_group)
