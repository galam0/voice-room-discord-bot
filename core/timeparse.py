"""'오늘 21시', '내일 9:30' 같은 시간 글자 해석"""

from datetime import datetime
from datetime import time as dtime
from datetime import timedelta
import re

from core.config import TZ


TIME_RE = re.compile(
    r"^\s*(?:(?P<day>오늘|내일|모레)|(?P<mon>\d{1,2})\s*(?:[/.\-]|월)\s*(?P<dd>\d{1,2})\s*일?)?\s*"
    r"(?P<ampm>오전|오후|am|pm)?\s*(?P<h>\d{1,2})\s*(?:(?::|시)\s*(?:(?P<m>\d{1,2})\s*분?)?)?\s*(?P<ampm2>am|pm)?\s*$",
    re.IGNORECASE,
)


def parse_time(text: str, now: datetime):
    """'21:00', '오후 9시', '내일 20:30', '10/25 21:00' 같은 글자를 시간으로. 못 읽으면 None.
    '오늘 오후 3시'처럼 날짜를 정해 준 시간은 이미 지났어도 그대로 돌려줘요 (부르는 쪽에서 막음)."""
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
        if t < now - timedelta(days=180):  # 한참 지난 날짜면 내년 (예: 12월에 '1/5')
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
