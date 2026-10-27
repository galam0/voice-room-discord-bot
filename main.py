"""
통화방 자동 생성 디스코드 봇 (Join to Create) - 실행 파일

- 생성 채널에 들어가면 "OOO의 통화방"을 만들어 자동 이동
- 게임별 생성 채널: "➕ 발로란트 5인방"에 들어가면 "OOO의 발로란트"(인원 5명 제한)
- /통화방 생성 명령어로도 생성 가능
- 방이 생기면 음성 채널 채팅에 버튼 컨트롤 패널 (잠금·공개·허용·이름·인원·방장넘기기)
- 빈 방 자동 삭제, 방장이 나가면 자동 위임, 한 사람당 방 1개
- /구인: 지금 있는 통화방으로 사람 구하는 이미지 글 (입장 버튼, 인원 자동 갱신, 방 비면 자동 마감)
- 게임은 관리자가 등록한 것만 쓸 수 있고, 명령어에서 자동완성으로 고름

파일 구조
- core/   여러 기능이 같이 쓰는 것 (설정, 저장소, 봇 객체, 웹후크, 도우미)
- cogs/   기능별 명령어와 이벤트 (통화방, 구인, 예약, 추첨, 역할, 입장 인증, 설정, 도움말)
- cards/  이미지 카드 그리기
"""

from core.client import bot
from core.config import TOKEN

# 기능 파일을 불러오면 그 안의 명령어·이벤트가 bot에 등록돼요
import cogs.rooms  # noqa: F401
import cogs.recruit  # noqa: F401
import cogs.reservation  # noqa: F401
import cogs.giveaway  # noqa: F401
import cogs.roles  # noqa: F401
import cogs.settings  # noqa: F401
import cogs.verify  # noqa: F401
import cogs.help  # noqa: F401
import cogs.events  # noqa: F401


def run():
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN 환경변수가 없어요. .env 파일을 확인해 주세요.")
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    run()
