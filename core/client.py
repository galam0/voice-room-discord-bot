"""디스코드 봇 객체 (모든 파일이 이 bot을 같이 씀)"""

import os

from discord.ext import commands
import discord

import core.config  # noqa: F401  .env를 먼저 불러와야 아래 MEMBERS_INTENT를 읽을 수 있어요


intents = discord.Intents.default()
intents.voice_states = True
# 들어올 때 자동 역할을 쓰려면 개발자 포털 Bot → Server Members Intent를 켜고 .env에 MEMBERS_INTENT=1
intents.members = os.getenv("MEMBERS_INTENT", "0") == "1"
bot = commands.Bot(command_prefix="!", intents=intents)
