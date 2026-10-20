# 통화방 자동 생성 봇

"➕ 통화방 만들기"에 들어가면 **OOO의 통화방**이 생기고, 비면 자동으로 사라지는 디스코드 봇.

## 명령어

| 명령어 | 설명 |
|---|---|
| `/통화방 생성 [이름] [인원]` | 내 방 만들기 (음성 채널에 있으면 바로 이동) |
| `/통화방 이름 <새이름>` | 방 이름 변경 (디스코드 제한: 10분에 2번) |
| `/통화방 인원 <0~99>` | 인원 제한 (0 = 제한 없음) |
| `/통화방 잠금` | 지금 있는 사람 말고는 못 들어오게 |
| `/통화방 공개` | 잠금 해제 |
| `/통화방 허용 <멤버>` | 잠긴 방에 특정 사람 들이기 |
| `/통화방 방장넘기기 <멤버>` | 같은 방 사람에게 방장 넘기기 |
| `/통화방 정보` | 방장·인원·잠금 상태 보기 |
| `/구인 <내용> <인원> [게임] [멘션역할]` | 지금 있는 통화방으로 같이 할 사람 구하는 글 올리기 |
| `/구인마감` | 내 구인 글 마감 (글의 '마감' 버튼도 가능) |
| `/통화방설정 구인채널 [채널]` | (관리자) 구인 글이 올라갈 채널 지정 (비우면 해제) |
| `/통화방설정 만들기` | (관리자) 카테고리 + 생성 채널 자동 생성 |
| `/통화방설정 지정 <채널>` | (관리자) 기존 음성 채널을 생성 채널로 |
| `/통화방설정 해제` | (관리자) 생성 채널 지정 해제 |

구인 글은 **이미지 카드**로 올라가고(참가자 프로필 사진, 빈 자리, 모집 상태 표시) **🔊 통화방 들어가기** 버튼이 붙어요. 통화방 인원이 바뀌면 이미지가 자동으로 다시 그려져요. 목표를 채우면 "모집 완료", 방이 비거나 사라지면 자동 마감.

자동 기능: 빈 방 삭제 · 방장이 나가면 남은 사람에게 위임(기본 이름이면 새 방장 이름으로 바뀜) · 한 사람당 방 1개.

## 1. 봇 만들기

1. https://discord.com/developers/applications → New Application → Bot → **Reset Token**으로 토큰 복사
2. OAuth2 → URL Generator
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: **View Channels, Send Messages, Connect, Manage Channels, Manage Roles, Move Members**
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

`.env`와 `data.json`(서버별 설정)은 깃에 안 올라가니까 `git pull` 해도 그대로 남아요.

## 3. 서버에서 처음 설정

서버에서 `/통화방설정 만들기` 한 번 → "➕ 통화방 만들기" 채널에 들어가 보기.
