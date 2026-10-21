"""등록된 게임 고르기, 티어 자동완성"""

from discord import app_commands
import discord

from core.store import game_key, store


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
    msg = (f"**{name}**은(는) 등록 안 된 게임이에요! 목록에서 골라 주세요.\n"
           f"등록된 게임: {names}\n(관리자는 `/통화방설정 게임추가`로 등록할 수 있어요)")
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    return False


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
