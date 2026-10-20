#!/bin/bash
# GitHub에 새 커밋이 있으면 받아오고 봇을 재시작해요. (cron으로 몇 분마다 실행)
cd "$(dirname "$0")" || exit 1

git fetch -q origin main || exit 0
if [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]; then
    exit 0  # 새 버전 없음
fi

echo "[$(date '+%F %T')] 새 버전 발견: $(git log -1 --format=%s origin/main)"
git pull -q --ff-only origin main || { echo "pull 실패"; exit 1; }
./venv/bin/pip install -q -r requirements.txt
systemctl restart voice-room-bot
echo "[$(date '+%F %T')] 업데이트 완료"
