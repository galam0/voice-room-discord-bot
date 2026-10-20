"""구인 글 이미지 카드 만들기 (Pillow)."""

import io
import os

from PIL import Image, ImageDraw, ImageFont

W = 1000
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
}
STATUS_TEXT = {"open": "모집 중", "full": "모집 완료", "closed": "마감"}

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
    """한글은 띄어쓰기가 적어서 글자 단위로 줄바꿈."""
    lines, line = [], ""
    for ch in text.replace("\n", " "):
        if text_w(draw, line + ch, f) > max_w:
            lines.append(line)
            line = ch.lstrip()
        else:
            line += ch
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = ellipsize(draw, lines[-1] + "…", f, max_w)
    return lines


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
        d.text((size / 2, size / 2), (name or "?")[:1], font=font(size // 2, True), fill=(255, 255, 255), anchor="mm")
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
        ImageDraw.Draw(face).text((size / 2, size / 2), (name or "?")[:1].upper(),
                                  font=font(40, True), fill=(255, 255, 255), anchor="mm")
    else:
        bg = Image.new("RGBA", (size, size), PANEL + (255,))
        face = Image.alpha_composite(bg, face)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(face, (0, 0), mask)
    return out


def render_card(*, title: str, content: str, author: str, voice_name: str, target: int,
                members: list[tuple[str, bytes | None]], status: str, icon: bytes | None = None) -> io.BytesIO:
    """
    title: 게임 이름 (없으면 '구인')
    members: [(이름, 아바타 PNG 바이트 또는 None), ...]
    status: 'open' | 'full' | 'closed'
    icon: 서버에서 등록한 게임 아이콘 이미지 바이트 (없으면 첫 글자 아이콘)
    """
    accent = COLORS[status]
    tmp = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    f_title = font(44, True)
    lines = wrap(tmp, content, f_title, W - PAD * 2, 3)

    slots = min(max(target, len(members)), MAX_SLOTS)
    per_row = 8
    rows = (slots + per_row - 1) // per_row
    height = PAD + ICON + 28 + len(lines) * 60 + 20 + 34 + 36 + 1 + 36 + 34 + 20 + rows * (SLOT + 46) + PAD - 10

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
        st += f" · {max(target - len(members), 0)}명 더"
    f_st = font(24, True)
    st_w = text_w(d, st, f_st) + 36
    pill(d, W - PAD - st_w, y + (ICON - 44) / 2, st, f_st, PANEL, color=accent)
    title_x = PAD + ICON + 20
    d.text((title_x, y + ICON / 2), ellipsize(d, title, font(32, True), W - PAD - st_w - 20 - title_x),
           font=font(32, True), fill=TEXT, anchor="lm")

    # 내용
    y += ICON + 28
    for line in lines:
        d.text((PAD, y), line, font=f_title, fill=TEXT if status != "closed" else SUB)
        y += 60

    # 구인자 · 통화방
    y += 20
    f_sub = font(26)
    meta = f"구인자 {author}   ·   음성 {voice_name}"
    d.text((PAD, y), ellipsize(d, meta, f_sub, W - PAD * 2), font=f_sub, fill=SUB)
    y += 34 + 36
    d.line((PAD, y, W - PAD, y), fill=(63, 65, 71), width=2)
    y += 1 + 36

    # 인원
    f_label = font(26, True)
    d.text((PAD, y), "인원", font=f_label, fill=SUB)
    count = f"{len(members)} / {target}"
    d.text((W - PAD, y), count, font=font(30, True), fill=TEXT, anchor="ra")
    y += 34 + 20

    gap = (W - PAD * 2 - per_row * SLOT) / (per_row - 1)
    f_name = font(20)
    for i in range(slots):
        r, c = divmod(i, per_row)
        x = PAD + c * (SLOT + gap)
        sy = y + r * (SLOT + 46)
        cx = x + SLOT / 2
        if i < len(members):
            name, data = members[i]
            img.paste(circle_avatar(data, name, SLOT, accent), (int(x), int(sy)),
                      circle_avatar(data, name, SLOT, accent))
            if i == 0:  # 첫 번째 = 구인자
                d.ellipse((x - 3, sy - 3, x + SLOT + 3, sy + SLOT + 3), outline=(240, 178, 50), width=4)
            d.text((cx, sy + SLOT + 10), ellipsize(d, name, f_name, SLOT + gap - 6), font=f_name, fill=TEXT, anchor="ma")
        else:
            d.ellipse((x, sy, x + SLOT, sy + SLOT), outline=(78, 80, 88), width=3)
            d.text((cx, sy + SLOT / 2), "+", font=font(40), fill=MUTED, anchor="mm")
            d.text((cx, sy + SLOT + 10), "빈 자리", font=f_name, fill=MUTED, anchor="ma")

    if target > MAX_SLOTS:
        d.text((W - PAD, y + rows * (SLOT + 46) - 10), f"외 {target - MAX_SLOTS}자리", font=f_name, fill=MUTED, anchor="rs")

    if status == "closed":
        overlay = Image.new("RGBA", img.size, (30, 31, 34, 110))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    buf.seek(0)
    return buf
