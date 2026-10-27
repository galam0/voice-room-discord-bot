"""인증 설정·신청 기록 (core.store의 data.json에 같이 저장돼요)

data.json 구조
    guilds[서버ID]["verify"]:
        channel:       인증 채널ID
        panel:         안내 패널 글ID (panel_channel: 패널이 올라간 채널ID)
        notify:        승인 신청이 올라올 관리자 채널ID
        admin_role:    신청이 오면 멘션할 관리자 역할ID
        member_role:   승인되면 받는 '정상인' 역할ID
        male_role / female_role:  성별 역할ID
        game_channel:  승인 뒤 안내할 게임 역할 채널ID
        title / desc:  패널 문구 (비우면 기본 문구)
    verify_pending["서버ID:유저ID"]:
        {"nick", "birth"(출생년도), "gender"("m"/"f"/None), "msg"(신청 글ID), "channel"(신청 글 채널ID)}
"""

from core.store import store


# 이게 다 채워져야 인증을 받을 수 있어요
REQUIRED = {
    "member_role": "정상인 역할",
    "admin_role": "관리자 역할",
    "male_role": "남성 역할",
    "female_role": "여성 역할",
    "notify": "승인 알림 채널",
}


def cfg(guild_id: int) -> dict:
    return store.guild(guild_id).setdefault("verify", {})


def missing(guild_id: int) -> list[str]:
    """아직 안 정해진 필수 설정 이름들."""
    c = cfg(guild_id)
    return [name for key, name in REQUIRED.items() if not c.get(key)]


def save():
    store.save()


# ─────────────────────────────── 신청 기록 ───────────────────────────────
def _all() -> dict:
    return store.data.setdefault("verify_pending", {})


def _key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}:{user_id}"


def pending(guild_id: int, user_id: int):
    return _all().get(_key(guild_id, user_id))


def set_pending(guild_id: int, user_id: int, info: dict):
    _all()[_key(guild_id, user_id)] = info
    store.save()


def remove_pending(guild_id: int, user_id: int):
    if _all().pop(_key(guild_id, user_id), None) is not None:
        store.save()


def remove_pending_by_message(message_id: int):
    """관리자가 신청 글을 지웠으면 그 신청도 정리 (그래야 다시 신청할 수 있어요)."""
    keys = [k for k, v in _all().items() if v.get("msg") == message_id]
    for k in keys:
        del _all()[k]
    if keys:
        store.save()


def count_pending(guild_id: int) -> int:
    prefix = f"{guild_id}:"
    return sum(1 for k in _all() if k.startswith(prefix))
