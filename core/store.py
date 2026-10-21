"""data.json 저장/불러오기"""

import json
import os

from core.config import DATA_FILE


def game_key(name: str) -> str:
    """'League of Legends', 'league of legends' 를 같은 게임으로 보기."""
    return "".join(name.lower().split())


# ─────────────────────────────── 저장소 ───────────────────────────────
class Store:
    """
    data.json 구조
    guilds[서버ID]:
        hubs:   {생성채널ID: {"game": 게임 이름 또는 None, "limit": 인원 제한}}
        games:  {게임키: {"name": 표시 이름, "file": 아이콘 파일 경로 또는 None}}
        recruit: 구인 글 채널ID
    rooms[방ID]: {"owner", "guild", "custom_name", "locked", "game"}
    posts[음성채널ID]: 구인 글 정보
    """

    def __init__(self, path: str):
        self.path = path
        self.data = {"guilds": {}, "rooms": {}, "posts": {}, "menus": {}, "reservations": {}, "giveaways": {}}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        for key in ("guilds", "rooms", "posts", "menus", "reservations", "giveaways"):
            self.data.setdefault(key, {})
        self._migrate()

    def _migrate(self):
        """이전 버전 data.json(생성 채널 1개, game_icons) 자동 변환."""
        changed = False
        for g in self.data["guilds"].values():
            if "hub" in g:
                hub = g.pop("hub")
                if hub:
                    g.setdefault("hubs", {})[str(hub)] = {"game": None, "limit": 0}
                changed = True
            if "game_icons" in g:
                g.setdefault("games", {}).update(g.pop("game_icons"))
                changed = True
        if changed:
            self.save()

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def guild(self, guild_id: int) -> dict:
        return self.data["guilds"].setdefault(str(guild_id), {})

    # 생성 채널
    def hubs(self, guild_id: int) -> dict:
        return self.guild(guild_id).get("hubs", {})

    def set_hub(self, guild_id: int, channel_id: int, game=None, limit=0):
        self.guild(guild_id).setdefault("hubs", {})[str(channel_id)] = {"game": game, "limit": limit}
        self.save()

    def remove_hub(self, guild_id: int, channel_id: int) -> bool:
        removed = self.guild(guild_id).get("hubs", {}).pop(str(channel_id), None) is not None
        if removed:
            self.save()
        return removed

    # 방
    def room(self, channel_id: int):
        return self.data["rooms"].get(str(channel_id))

    def add_room(self, channel_id: int, owner_id: int, guild_id: int, custom_name: bool, game=None):
        self.data["rooms"][str(channel_id)] = {
            "owner": owner_id,
            "guild": guild_id,
            "custom_name": custom_name,  # 방장이 직접 이름을 정했으면 방장 바뀌어도 이름 유지
            "locked": False,
            "game": game,
        }
        self.save()

    def update_room(self, channel_id: int, **fields):
        self.data["rooms"][str(channel_id)].update(fields)
        self.save()

    def remove_room(self, channel_id: int):
        if self.data["rooms"].pop(str(channel_id), None) is not None:
            self.save()

    def room_of_owner(self, guild_id: int, user_id: int):
        for cid, r in self.data["rooms"].items():
            if r["guild"] == guild_id and r["owner"] == user_id:
                return int(cid)
        return None

    # 구인 채널
    def recruit_channel(self, guild_id: int):
        return self.guild(guild_id).get("recruit")

    def set_recruit_channel(self, guild_id: int, channel_id):
        self.guild(guild_id)["recruit"] = channel_id
        self.save()

    # 방이 생길 카테고리 (서버 기본값)
    def room_category(self, guild_id: int):
        return self.guild(guild_id).get("room_category")

    def set_room_category(self, guild_id: int, category_id):
        self.guild(guild_id)["room_category"] = category_id
        self.save()

    def set_hub_category(self, guild_id: int, hub_id: int, category_id) -> bool:
        hub = self.hubs(guild_id).get(str(hub_id))
        if hub is None:
            return False
        hub["category"] = category_id
        self.save()
        return True

    # 구인 글 (음성 채널 하나당 글 하나)
    def post(self, voice_id: int):
        return self.data["posts"].get(str(voice_id))

    def set_post(self, voice_id: int, **info):
        self.data["posts"][str(voice_id)] = info
        self.save()

    def remove_post(self, voice_id: int):
        if self.data["posts"].pop(str(voice_id), None) is not None:
            self.save()

    # 등록된 게임
    def games(self, guild_id: int) -> dict:
        return self.guild(guild_id).get("games", {})

    def game(self, guild_id: int, name: str | None):
        return self.games(guild_id).get(game_key(name)) if name else None

    def set_game(self, guild_id: int, name: str, icon_file):
        games = self.guild(guild_id).setdefault("games", {})
        entry = games.setdefault(game_key(name), {})
        entry.update(name=name, file=icon_file)  # 연결된 역할 등 다른 설정은 유지
        self.save()

    def set_game_role(self, guild_id: int, name: str, role_id):
        self.games(guild_id)[game_key(name)]["role"] = role_id
        self.save()

    def remove_game(self, guild_id: int, name: str):
        self.guild(guild_id).get("games", {}).pop(game_key(name), None)
        self.save()

    # 역할 메뉴 {메시지ID: {"guild", "channel", "title", "desc", "items": {이모지키: {"emoji", "role"}}}}
    def menu(self, message_id: int):
        return self.data["menus"].get(str(message_id))

    def set_menu(self, message_id: int, menu: dict):
        self.data["menus"][str(message_id)] = menu
        self.save()

    def remove_menu(self, message_id: int):
        if self.data["menus"].pop(str(message_id), None) is not None:
            self.save()

    # 예약 {메시지ID: {...}}
    def res(self, message_id):
        return self.data["reservations"].get(str(message_id))

    def set_res(self, message_id, res: dict):
        self.data["reservations"][str(message_id)] = res
        self.save()

    def remove_res(self, message_id):
        if self.data["reservations"].pop(str(message_id), None) is not None:
            self.save()


store = Store(DATA_FILE)
