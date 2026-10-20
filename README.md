# 통화방 자동 생성 봇

"➕ 통화방 만들기"에 들어가면 **OOO의 통화방**이 생기고, 비면 자동으로 사라지는 디스코드 봇.

## 명령어

방이 생기면 음성 채널 채팅에 **컨트롤 패널**이 올라와요. 방장은 버튼으로 🔒잠금 · 🔓공개 · ✅허용 · ✏️이름 · 👥인원 · 👑방장넘기기를 할 수 있어요. 아래 명령어로도 똑같이 할 수 있어요.

| 명령어 | 설명 |
|---|---|
| `/통화방 생성 [이름] [인원] [게임]` | 내 방 만들기 (음성 채널에 있으면 바로 이동) |
| `/통화방 이름 <새이름>` | 방 이름 변경 (디스코드 제한: 10분에 2번) |
| `/통화방 인원 <0~99>` | 인원 제한 (0 = 제한 없음) |
| `/통화방 잠금` · `/통화방 공개` | 잠금 / 잠금 해제 |
| `/통화방 허용 <멤버>` | 잠긴 방에 특정 사람 들이기 |
| `/통화방 방장넘기기 <멤버>` | 같은 방 사람에게 방장 넘기기 |
| `/통화방 패널` | 컨트롤 패널 다시 올리기 |
| `/통화방 정보` | 방장·인원·잠금 상태 보기 |
| `/구인 <내용> <인원> [게임] [티어] [멘션역할]` | 지금 있는 통화방으로 같이 할 사람 구하는 이미지 글 |
| `/도움말` | 명령어 안내 (관리자에게는 관리자 명령어도 보여요) |
| `/구인마감` | 내 구인 글 마감 (글의 '마감' 버튼도 가능) |

### 관리자 (`/통화방설정`)

| 명령어 | 설명 |
|---|---|
| `만들기` | 카테고리 + "➕ 통화방 만들기" 생성 |
| `게임채널 <게임> <인원> [채널이름]` | 게임별 생성 채널 만들기 (예: "➕ 발로란트 5인방" → "OOO의 발로란트", 5명 제한) |
| `지정 <채널> [게임] [인원]` | 기존 음성 채널을 생성 채널로 |
| `해제 <채널>` | 생성 채널 지정 해제 (채널은 안 지움) |
| `게임추가 <게임> [아이콘]` | 게임 등록 (아이콘은 선택, 다시 하면 아이콘만 교체) |
| `게임삭제 <게임>` | 등록한 게임 삭제 |
| `카테고리 [카테고리] [생성채널]` | 방이 만들어질 카테고리 (생성채널을 고르면 그 채널로 만든 방에만 적용, 카테고리를 비우면 해제) |
| `구인채널 [채널]` | 구인 글이 올라갈 채널 (비우면 해제) |
| `목록` | 생성 채널 · 등록된 게임 · 구인 채널 보기 |

**게임은 등록된 것만** 쓸 수 있고, 게임을 넣는 칸은 전부 자동완성 목록에서 골라요. 게임 방에서 `/구인`을 쓰면 게임이 자동으로 채워져요.

`/구인`의 **티어**는 롤·발로란트·오버워치면 자동완성으로 추천돼요 (`골드~`까지 치면 `골드~플래티넘` 같은 범위도 추천). 다른 게임은 직접 적으면 돼요. 구인 글의 **🙋 참가할게요** 버튼을 누르면 이미지에 "예정"으로 표시되고 통화방 채팅으로 구인자에게 알림이 가요 (다시 누르면 취소, 통화방에 들어오면 자동으로 정식 인원으로). 구인 글은 **3시간 뒤 자동 마감**돼요 (`.env`의 `RECRUIT_EXPIRE_HOURS`로 변경, 0이면 끔).

구인 글은 **이미지 카드**로 올라가고(게임 아이콘, 티어, 참가자 프로필 사진, 빈 자리, 모집 상태) **🔊 통화방 들어가기** 버튼이 붙어요. 인원이 바뀌면 이미지가 다시 그려지고, 목표를 채우면 "모집 완료", 방이 비거나 사라지면 자동 마감.

자동 기능: 빈 방 삭제 · 방장이 나가면 남은 사람에게 위임(기본 이름이면 새 방장 이름으로 바뀜) · 한 사람당 방 1개.

## 1. 봇 만들기

1. https://discord.com/developers/applications → New Application → Bot → **Reset Token**으로 토큰 복사
2. OAuth2 → URL Generator
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: **View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Connect, Manage Channels, Manage Roles, Move Members**
   - 또는 초대 링크를 직접: `https://discord.com/oauth2/authorize?client_id=애플리케이션ID&permissions=286379024&scope=bot%20applications.commands`
3. 만들어진 링크로 서버에 초대
4. 서버 설정 → 역할에서 봇 역할이 너무 아래 있지 않게 (잠금 기능이 권한을 수정해야 함)

## 2. 구글 클라우드 VM에서 실행

VM에 SSH로 접속해서:

```bash
cd ~
git clone https://github.com/galam0/voice-room-discord-bot.git voice-room-bot
cd ~/voice-room-bot
sudo apt install -y python3-venv fonts-noto-cjk   # 구인 이미지에 쓸 한글 폰트 포함
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env    # DISCORD_TOKEN 넣기
./venv/bin/python bot.py   # 테스트 실행, 확인되면 Ctrl+C
```

### 항상 켜 두기 (systemd)

`/etc/systemd/system/voice-room-bot.service`:

```ini
[Unit]
Description=Voice room Discord bot
After=network-online.target

[Service]
WorkingDirectory=/home/사용자명/voice-room-bot
ExecStart=/home/사용자명/voice-room-bot/venv/bin/python bot.py
Restart=always
RestartSec=5
User=사용자명

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now voice-room-bot
journalctl -u voice-room-bot -f   # 로그 보기
```

다른 봇들을 다른 방식(pm2, screen 등)으로 켜 두고 있다면 같은 방식으로 `venv/bin/python bot.py`를 등록해도 돼요.

### 코드 업데이트할 때

```bash
cd ~/voice-room-bot && git pull && ./venv/bin/pip install -r requirements.txt
sudo systemctl restart voice-room-bot
```

### 자동 업데이트 (선택)

GitHub에 올라온 새 버전을 5분마다 확인해서 자동으로 받고 재시작하게 하려면 한 번만:

```bash
chmod +x /root/voice-room-bot/auto-update.sh
(crontab -l 2>/dev/null; echo "*/5 * * * * /root/voice-room-bot/auto-update.sh >> /root/voice-room-bot/update.log 2>&1") | crontab -
```

업데이트 기록은 `cat /root/voice-room-bot/update.log`, 끄려면 `crontab -e`에서 그 줄을 지우면 돼요.

`.env`와 `data.json`(서버별 설정)은 깃에 안 올라가니까 `git pull` 해도 그대로 남아요.

## 3. 서버에서 처음 설정

1. `/통화방설정 만들기` → "➕ 통화방 만들기" 채널에 들어가 보기
2. `/통화방설정 게임추가`로 게임 등록 (아이콘 첨부 가능)
3. 원하면 `/통화방설정 게임채널`로 게임별 생성 채널, `/통화방설정 구인채널`로 구인 채널 지정
