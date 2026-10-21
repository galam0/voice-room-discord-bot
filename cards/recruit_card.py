"""구인 글 이미지 카드 만들기 (Pillow)."""

import io
import os
import re

from PIL import Image, ImageDraw, ImageFont

W = 1000           # 그리는 크기 (마지막에 CARD_WIDTH로 줄여서 보냄)
# 디스코드에 올라가는 카드 가로 크기(px). 작을수록 채팅에서 작게 보여요
CARD_WIDTH = int(os.getenv("CARD_WIDTH", "600"))
PAD = 48
SLOT = 88          # 인원 칸 원 크기
MAX_SLOTS = 10     # 이미지에 그릴 최대 칸 수

BG = (43, 45, 49)
PANEL = (30, 31, 34)
TEXT = (242, 243, 245)
SUB = (181, 186, 193)
MUTED = (128, 132, 142)
COLORS = {
    "open": (88, 101, 242),    # 모집 중
    "full": (35, 165, 90),     # 모집 완료
    "closed": (128, 132, 142), # 마감
    "started": (35, 165, 90),  # 예약 시작
    "cancelled": (128, 132, 142),  # 예약 취소
}
STATUS_TEXT = {"open": "모집 중", "full": "모집 완료", "closed": "마감", "started": "시작됨", "cancelled": "취소됨"}
FADED = ("closed", "cancelled")  # 흐리게 그리는 상태

# 한글 폰트 후보 (VM에는 `sudo apt install fonts-noto-cjk` 또는 fonts-nanum)
FONT_CANDIDATES = {
    "regular": [
        (os.getenv("FONT_PATH", ""), 0),
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 1),
        ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf", 0),
        ("C:/Windows/Fonts/malgun.ttf", 0),
    ],
    "bold": [
        (os.getenv("FONT_BOLD_PATH", ""), 0),
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 1),
        ("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf", 0),
        ("C:/Windows/Fonts/malgunbd.ttf", 0),
    ],
}
_font_cache = {}


def font(size: int, bold: bool = False):
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    for path, index in FONT_CANDIDATES["bold" if bold else "regular"]:
        if path and os.path.exists(path):
            f = ImageFont.truetype(path, size, index=index)
            break
    else:
        raise RuntimeError("한글 폰트를 찾지 못했어요. VM에서 `sudo apt install fonts-noto-cjk`를 실행해 주세요.")
    _font_cache[key] = f
    return f


def text_w(draw, text, f):
    return draw.textlength(text, font=f)


def ellipsize(draw, text, f, max_w):
    if text_w(draw, text, f) <= max_w:
        return text
    while text and text_w(draw, text + "…", f) > max_w:
        text = text[:-1]
    return text + "…"


def wrap(draw, text, f, max_w, max_lines):
    """띄어쓰기 기준으로 줄바꿈, 한 단어가 너무 길면 글자 단위로 자름."""
    lines, line = [], ""
    for word in text.replace("\n", " ").split(" "):
        candidate = f"{line} {word}" if line else word
        if text_w(draw, candidate, f) <= max_w:
            line = candidate
            continue
        if line:
            lines.append(line)
        line = ""
        for ch in word:  # 긴 단어는 글자 단위로
            if text_w(draw, line + ch, f) > max_w:
                lines.append(line)
                line = ch
            else:
                line += ch
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = ellipsize(draw, lines[-1] + "…", f, max_w)
    return lines


# ─────────────── 이모지 섞인 글자 그리기 ───────────────
# 기본 이모지는 컬러 이모지 폰트(VM: sudo apt install fonts-noto-color-emoji),
# 서버 이모지(<:이름:ID>)는 봇이 받아 둔 이미지로 그려요.
EMOJI_FONT_CANDIDATES = [
    os.getenv("EMOJI_FONT_PATH", ""),
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "C:/Windows/Fonts/seguiemj.ttf",
]
_PIC = "[\U0001F000-\U0001FAFF]"
_MOD = "[\uFE0F\U0001F3FB-\U0001F3FF]*"
TOKEN_RE = re.compile(
    r"<a?:(?P<cname>\w{2,32}):(?P<cid>\d{15,25})>"                       # 서버 이모지
    r"|(?P<emoji>[\U0001F1E6-\U0001F1FF]{2}"                               # 국기
    r"|[0-9#*]\uFE0F?\u20E3"                                               # 1️⃣ 같은 키캡
    rf"|(?:{_PIC}{_MOD}|[\u2000-\u2BFF]\uFE0F{_MOD})"                      # 이모지 (+피부색)
    rf"(?:\u200D(?:{_PIC}|[\u2000-\u2BFF])\uFE0F?{_MOD})*)"               # 👨‍👩‍👧 같은 조합
)
_INVISIBLE = re.compile("[\uFE0F\u200D\U000E0020-\U000E007F]")
_emoji_font = None
_emoji_cache = {}


def emoji_font():
    global _emoji_font
    if _emoji_font is None:
        _emoji_font = False
        for path in EMOJI_FONT_CANDIDATES:
            if path and os.path.exists(path):
                try:
                    _emoji_font = ImageFont.truetype(path, 109)  # 컬러 이모지 폰트는 109 크기만 지원
                    break
                except OSError:
                    continue
    return _emoji_font or None


_glyph_cache = {}


def _cjk_has_glyph(ch: str) -> bool:
    """한글 폰트에 그 글자가 있는지 (없으면 네모가 그려짐)."""
    if ch not in _glyph_cache:
        def shape(c):
            im = Image.new("L", (64, 64), 0)
            ImageDraw.Draw(im).text((4, 4), c, font=font(40), fill=255)
            return im.tobytes()
        if "missing" not in _glyph_cache:
            _glyph_cache["missing"] = shape("\U0010FFFD")
        _glyph_cache[ch] = shape(ch) != _glyph_cache["missing"]
    return _glyph_cache[ch]


def atoms(text: str, custom: dict | None = None):
    """글자를 ("t", 글자) / ("e", 이모지) / ("c", 이미지 바이트) 조각으로 나눔."""
    out = []

    def add_text(t):
        for ch in _INVISIBLE.sub("", t):
            # ✨ 처럼 한글 폰트에 없는 기호는 이모지로 그림
            if "\u2000" <= ch <= "\u2BFF" and emoji_font() and not _cjk_has_glyph(ch):
                out.append(("e", ch))
            else:
                out.append(("t", ch))

    pos = 0
    for m in TOKEN_RE.finditer(text or ""):
        add_text(text[pos:m.start()])
        if m.group("cid"):
            data = (custom or {}).get(m.group("cid"))
            if data:
                out.append(("c", data))
            else:
                add_text(f":{m.group('cname')}:")
        elif emoji_font():
            out.append(("e", m.group("emoji")))
        pos = m.end()
    add_text(text[pos:] if text else "")
    return out


def _esize(f):
    return int(f.size * 1.08)


def _runs(at):
    buf = ""
    for kind, val in at:
        if kind == "t":
            buf += val
            continue
        if buf:
            yield "t", buf
            buf = ""
        yield kind, val
    if buf:
        yield "t", buf


def rich_w(draw, at, f):
    return sum(draw.textlength(v, font=f) if k == "t" else _esize(f) + 4 for k, v in _runs(at))


def rich_ellipsize(draw, at, f, max_w):
    if rich_w(draw, at, f) <= max_w:
        return at
    at = list(at)
    while at and rich_w(draw, at + [("t", "…")], f) > max_w:
        at.pop()
    return at + [("t", "…")]


def rich_wrap(draw, at, f, max_w, max_lines):
    """띄어쓰기 기준으로 줄바꿈, 한 단어가 너무 길면 조각 단위로 자름."""
    words, cur = [], []
    for a in at:
        if a == ("t", " ") or a == ("t", "\n"):
            words.append(cur)
            cur = []
        else:
            cur.append(a)
    words.append(cur)
    lines, line = [], []
    for word in words:
        candidate = line + [("t", " ")] + word if line else word
        if rich_w(draw, candidate, f) <= max_w:
            line = candidate
            continue
        if line:
            lines.append(line)
        line = []
        for a in word:  # 긴 단어는 조각 단위로
            if rich_w(draw, line + [a], f) > max_w:
                lines.append(line)
                line = [a]
            else:
                line.append(a)
    if line:
        lines.append(line)
    lines = [ln for ln in lines if ln] or [[]]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = rich_ellipsize(draw, lines[-1] + [("t", "…")], f, max_w)
    return lines


def _emoji_image(kind, val, size):
    key = (kind, val if kind == "e" else hash(val), size)
    if key in _emoji_cache:
        return _emoji_cache[key]
    im = None
    try:
        if kind == "e":
            canvas = Image.new("RGBA", (320, 160), (0, 0, 0, 0))
            ImageDraw.Draw(canvas).text((0, 0), val, font=emoji_font(), embedded_color=True)
            box = canvas.getbbox()
            im = canvas.crop(box) if box else None
        else:
            im = Image.open(io.BytesIO(val)).convert("RGBA")
    except Exception:
        im = None
    if im is not None:
        scale = size / max(im.size)
        im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
    _emoji_cache[key] = im
    return im


def rich_draw(img, draw, xy, at, f, fill, anchor="la"):
    """anchor: 가로 l/m/r + 세로 a(위)/m(가운데)."""
    x, y = xy
    width = rich_w(draw, at, f)
    x -= {"l": 0, "m": width / 2, "r": width}[anchor[0]]
    ascent, descent = f.getmetrics()
    baseline = y + ascent if anchor[1] == "a" else y + (ascent - descent) / 2
    size = _esize(f)
    for kind, val in _runs(at):
        if kind == "t":
            draw.text((x, baseline), val, font=f, fill=fill, anchor="ls")
            x += draw.textlength(val, font=f)
            continue
        em = _emoji_image(kind, val, size)
        if em is not None:
            center_y = baseline - ascent * 0.36
            img.paste(em, (int(x + 2 + (size - em.width) / 2), int(center_y - em.height / 2)), em)
        x += size + 4


def initial(name: str) -> str:
    for ch in name or "":
        if ch.isalnum():
            return ch.upper()
    return "?"


def pill(draw, x, y, text, f, fill, color=(255, 255, 255), pad_x=18, h=44):
    w = text_w(draw, text, f) + pad_x * 2
    draw.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=fill)
    draw.text((x + pad_x, y + h / 2), text, font=f, fill=color, anchor="lm")
    return w


def circle_avatar(data: bytes | None, name: str, size: int, color):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    face = None
    if data:
        try:
            face = Image.open(io.BytesIO(data)).convert("RGBA").resize((size, size), Image.LANCZOS)
        except Exception:
            face = None
    if face is None:
        face = Image.new("RGBA", (size, size), color + (255,))
        d = ImageDraw.Draw(face)
        d.text((size / 2, size / 2), initial(name), font=font(size // 2, True), fill=(255, 255, 255), anchor="mm")
    img.paste(face, (0, 0), mask)
    return img


ICON = 76  # 게임 아이콘 크기


def game_icon(data: bytes | None, name: str, color):
    """등록된 아이콘(둥근 사각형) 또는 첫 글자 기본 아이콘."""
    size = ICON
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=18, fill=255)
    face = None
    if data:
        try:
            face = Image.open(io.BytesIO(data)).convert("RGBA").resize((size, size), Image.LANCZOS)
        except Exception:
            face = None
    if face is None:
        face = Image.new("RGBA", (size, size), color + (255,))
        ImageDraw.Draw(face).text((size / 2, size / 2), initial(name),
                                  font=font(40, True), fill=(255, 255, 255), anchor="mm")
    else:
        bg = Image.new("RGBA", (size, size), PANEL + (255,))
        face = Image.alpha_composite(bg, face)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(face, (0, 0), mask)
    return out


def render_card(*, title: str, content: str, author: str, voice_name: str, target: int,
                members: list[tuple[str, bytes | None]], status: str, icon: bytes | None = None,
                tags: list[str] | None = None, applicants: list[tuple[str, bytes | None]] | None = None,
                footer: str | None = None, custom_emojis: dict | None = None,
                meta: str | None = None, remaining: int | None = None,
                count_pending: bool = False) -> io.BytesIO:
    """
    title: 게임 이름 (없으면 '구인')
    members: 통화방에 있는 사람 [(이름, 아바타 PNG 바이트 또는 None), ...]
    applicants: 참가 신청한 사람 (아직 통화방엔 없음)
    status: 'open' | 'full' | 'closed'
    icon: 서버에서 등록한 게임 아이콘 이미지 바이트 (없으면 첫 글자 아이콘)
    tags: 티어·포지션 같은 태그
    footer: 맨 아래 작은 글씨 (예: 자동 마감 시간)
    custom_emojis: 서버 이모지 {ID: 이미지 바이트}
    remaining: '모집 중 · N명 더'의 N (안 주면 target - members)
    count_pending: True면 applicants도 인원 수에 넣고 '예정' 글자 없이 흐리게만 그림 (예약용)
    """
    tags = [t for t in (tags or []) if t]
    applicants = applicants or []
    accent = COLORS[status]
    tmp = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    f_title = font(44, True)
    lines = rich_wrap(tmp, atoms(content, custom_emojis), f_title, W - PAD * 2 - 64, 3)

    people = members + applicants
    slots = min(max(target, len(people)), MAX_SLOTS)
    per_row = 8
    rows = (slots + per_row - 1) // per_row
    height = (PAD + ICON + 28 + len(lines) * 60 + 36 + (20 + 44 if tags else 0) + 20 + 34 + 24 + 1 + 24 + 34 + 20
              + rows * (SLOT + 46) + (48 if footer else 0) + PAD - 10)

    img = Image.new("RGB", (W, height), BG)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, 10), fill=accent)

    # 상단: 게임 아이콘 + 게임 이름 + 상태
    y = PAD
    title = title or "구인"
    ic = game_icon(icon, title, accent)
    img.paste(ic, (PAD, y), ic)
    st = STATUS_TEXT[status]
    if status == "open":
        st += f" · {max(target - len(members) if remaining is None else remaining, 0)}명 더"
    f_st = font(24, True)
    st_w = text_w(d, st, f_st) + 36
    pill(d, W - PAD - st_w, y + (ICON - 44) / 2, st, f_st, PANEL, color=accent)
    title_x = PAD + ICON + 20
    rich_draw(img, d, (title_x, y + ICON / 2),
              rich_ellipsize(d, atoms(title), font(32, True), W - PAD - st_w - 20 - title_x),
              font(32, True), TEXT, anchor="lm")

    # 내용 (박스로 강조)
    y += ICON + 28
    box_h = len(lines) * 60 + 36
    d.rounded_rectangle((PAD, y, W - PAD, y + box_h), radius=16, fill=PANEL)
    d.rounded_rectangle((PAD, y, PAD + 8, y + box_h), radius=4, fill=accent)
    ty = y + 14
    for line in lines:
        rich_draw(img, d, (PAD + 36, ty), line, f_title, TEXT if status not in FADED else SUB)
        ty += 60
    y += box_h

    # 티어·포지션 태그
    if tags:
        y += 20
        x = PAD
        f_tag = font(24, True)
        for tag in tags:
            tag = ellipsize(d, tag, f_tag, 360)
            w = text_w(d, tag, f_tag) + 36
            if x + w > W - PAD:
                break
            pill(d, x, y, tag, f_tag, PANEL, color=TEXT)
            x += w + 12
        y += 44

    # 구인자 · 통화방
    y += 20
    f_sub = font(26)
    meta = meta or f"구인자 {author}   ·   음성 {voice_name}"
    rich_draw(img, d, (PAD, y), rich_ellipsize(d, atoms(meta), f_sub, W - PAD * 2), f_sub, SUB)
    y += 34 + 24
    d.line((PAD, y, W - PAD, y), fill=(63, 65, 71), width=2)
    y += 1 + 24

    # 인원
    f_label = font(26, True)
    d.text((PAD, y), "인원", font=f_label, fill=SUB)
    f_count = font(30, True)
    count = f"{len(members) + (len(applicants) if count_pending else 0)} / {target}"
    d.text((W - PAD, y), count, font=f_count, fill=TEXT, anchor="ra")
    if applicants and not count_pending:
        extra = f"+{len(applicants)} 예정   "
        d.text((W - PAD - text_w(d, count, f_count), y + 4), extra, font=font(24, True), fill=accent, anchor="ra")
    y += 34 + 20

    gap = (W - PAD * 2 - per_row * SLOT) / (per_row - 1)
    f_name = font(24)
    f_badge = font(20, True)
    for i in range(slots):
        r, c = divmod(i, per_row)
        x = PAD + c * (SLOT + gap)
        sy = y + r * (SLOT + 46)
        cx = x + SLOT / 2
        if i < len(people):
            name, data = people[i]
            av = circle_avatar(data, name, SLOT, accent)
            pending = i >= len(members)
            if pending:  # 참가 예정: 흐리게
                faded = av.copy()
                faded.putalpha(av.getchannel("A").point(lambda a: a * 45 // 100))
                img.paste(faded, (int(x), int(sy)), faded)
                d.ellipse((x, sy, x + SLOT, sy + SLOT), outline=accent, width=3)
                if not count_pending:
                    pill(d, cx - 30, sy + SLOT - 24, "예정", f_badge, accent, pad_x=10, h=32)
            else:
                img.paste(av, (int(x), int(sy)), av)
                if i == 0:  # 첫 번째 = 구인자
                    d.ellipse((x - 3, sy - 3, x + SLOT + 3, sy + SLOT + 3), outline=(240, 178, 50), width=4)
            rich_draw(img, d, (cx, sy + SLOT + 10), rich_ellipsize(d, atoms(name), f_name, SLOT + gap - 6),
                      f_name, SUB if pending else TEXT, anchor="ma")
        else:
            d.ellipse((x, sy, x + SLOT, sy + SLOT), outline=(78, 80, 88), width=3)
            d.text((cx, sy + SLOT / 2), "+", font=font(40), fill=MUTED, anchor="mm")
            d.text((cx, sy + SLOT + 10), "빈 자리", font=f_name, fill=MUTED, anchor="ma")

    if target > MAX_SLOTS:
        d.text((W - PAD, y + rows * (SLOT + 46) - 10), f"외 {target - MAX_SLOTS}자리", font=f_name, fill=MUTED, anchor="rs")

    if footer:
        d.text((PAD, y + rows * (SLOT + 46) + 14), footer, font=font(26), fill=MUTED)

    if status in FADED:
        overlay = Image.new("RGBA", img.size, (30, 31, 34, 110))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    if 0 < CARD_WIDTH < W:
        img = img.resize((CARD_WIDTH, round(img.height * CARD_WIDTH / W)), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    buf.seek(0)
    return buf
