"""환경변수(.env) 설정값과 로그"""

from zoneinfo import ZoneInfo
import logging
import os

from dotenv import load_dotenv


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
