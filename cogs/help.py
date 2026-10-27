"""/도움말"""

from discord import app_commands
import discord

from core.client import bot


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
        "참가 버튼을 누르면 통화방에 들어오기 전까지 예정으로 표시돼요. 인원이 바뀌면 이미지가 갱신되고, "
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
            f"{cmd('통화방설정 로그채널')} 관리자 기록 채널",
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
        admin.add_field(name="자동 역할", inline=False, value="\n".join([
            f"{cmd('자동역할 목록')} · {cmd('자동역할 추가')} · {cmd('자동역할 빼기')}",
        ]))
        admin.add_field(name="추첨 (🎉 반응)", inline=False, value="\n".join([
            f"{cmd('추첨 시작')} 추첨 글 올리기",
            f"{cmd('추첨 끝내기')} 지금 바로 끝내기",
            f"{cmd('추첨 다시뽑기')} 끝난 추첨 다시 뽑기",
        ]))
        admin.add_field(name="입장 인증 (닉네임·나이·성별 → 관리자 승인)", inline=False, value="\n".join([
            f"{cmd('인증 설정')} 정상인·관리자·성별 역할, 승인 알림·게임 역할 채널 정하기/바꾸기",
            f"{cmd('인증 채널만들기')} 인증 채널 만들고 안내 패널 올리기",
            f"{cmd('인증 패널')} 패널 다시 올리기·문구 바꾸기",
            f"{cmd('인증 정보')} 지금 설정 보기",
        ]))
        admin.set_footer(text="처음이면: 만들기 → 게임추가 → (게임채널·카테고리·구인채널)")
        embeds.append(admin)
    return embeds


@bot.tree.command(name="도움말", description="통화방 봇 명령어를 알려줘요")
@app_commands.guild_only()
async def cmd_help(interaction: discord.Interaction):
    is_admin = interaction.user.guild_permissions.manage_guild
    await interaction.response.send_message(embeds=help_embeds(is_admin), ephemeral=True)
