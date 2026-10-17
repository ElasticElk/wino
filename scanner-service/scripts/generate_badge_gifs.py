#!/usr/bin/env python3
"""Build the eight looping badge GIFs used on Telegram unlocks."""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 128
FRAMES = 12
BG = (22, 16, 20)
OUT = Path(__file__).resolve().parent.parent / "assets" / "badges"

FAMILIES = {
    "bottles": (196, 48, 78),
    "country": (42, 148, 152),
    "explorer": (62, 158, 86),
    "type": (148, 78, 186),
    "grape": (136, 52, 148),
    "producer": (176, 122, 52),
    "alphabet": (56, 90, 178),
    "decade": (204, 164, 48),
}


def _scale(t: int) -> float:
    # Pop in, slight overshoot, settle.
    u = t / (FRAMES - 1)
    if u < 0.45:
        return 0.35 + 0.9 * (u / 0.45)
    if u < 0.7:
        return 1.25 - 0.25 * ((u - 0.45) / 0.25)
    return 1.0


def _spark(draw: ImageDraw.ImageDraw, cx: int, cy: int, t: int, color: tuple):
    angle = t * (math.pi * 2 / FRAMES)
    r = 46
    x = int(cx + r * math.cos(angle))
    y = int(cy + r * math.sin(angle))
    draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=(255, 236, 180))
    x2 = int(cx + r * math.cos(angle + math.pi))
    y2 = int(cy + r * math.sin(angle + math.pi))
    draw.ellipse((x2 - 3, y2 - 3, x2 + 3, y2 + 3), fill=color)


def _circle(draw, cx, cy, r, fill):
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)


def _icon_bottles(draw, cx, cy, s, color):
    w, h = int(14 * s), int(36 * s)
    neck_w, neck_h = int(6 * s), int(14 * s)
    draw.rectangle((cx - neck_w // 2, cy - h // 2 - neck_h, cx + neck_w // 2, cy - h // 2), fill=color)
    draw.rounded_rectangle(
        (cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2),
        radius=max(3, int(4 * s)), fill=color,
    )


def _icon_country(draw, cx, cy, s, color):
    r = int(28 * s)
    _circle(draw, cx, cy, r, color)
    _circle(draw, cx, cy, int(r * 0.72), BG)
    _circle(draw, cx, cy, int(r * 0.55), color)
    draw.ellipse((cx - r, cy - int(10 * s), cx + r, cy + int(10 * s)), outline=BG, width=max(2, int(2 * s)))
    draw.line((cx, cy - r, cx, cy + r), fill=BG, width=max(2, int(2 * s)))


def _icon_explorer(draw, cx, cy, s, color):
    w, h = int(52 * s), int(36 * s)
    draw.rounded_rectangle((cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2), radius=6, fill=color)
    for dx, dy in ((-12, -4), (10, 6), (0, 8)):
        _circle(draw, cx + int(dx * s), cy + int(dy * s), max(3, int(4 * s)), (255, 236, 180))


def _icon_type(draw, cx, cy, s, color):
    bowl = int(22 * s)
    draw.polygon(
        [(cx - bowl, cy - int(16 * s)), (cx + bowl, cy - int(16 * s)), (cx, cy + int(10 * s))],
        fill=color,
    )
    stem_w = max(3, int(4 * s))
    draw.rectangle((cx - stem_w // 2, cy + int(8 * s), cx + stem_w // 2, cy + int(26 * s)), fill=color)
    draw.rectangle((cx - int(12 * s), cy + int(26 * s), cx + int(12 * s), cy + int(30 * s)), fill=color)


def _icon_grape(draw, cx, cy, s, color):
    r = max(6, int(9 * s))
    for dx, dy in ((-10, -6), (10, -6), (0, -12), (-6, 8), (6, 8), (0, 16)):
        _circle(draw, cx + int(dx * s), cy + int(dy * s), r, color)
    draw.line((cx, cy - int(22 * s), cx, cy - int(12 * s)), fill=(90, 160, 70), width=max(2, int(3 * s)))


def _icon_producer(draw, cx, cy, s, color):
    w, h = int(40 * s), int(28 * s)
    draw.rectangle((cx - w // 2, cy - 2, cx + w // 2, cy + h // 2), fill=color)
    draw.polygon(
        [(cx - w // 2 - 6, cy), (cx + w // 2 + 6, cy), (cx, cy - int(22 * s))],
        fill=color,
    )
    dw = int(8 * s)
    draw.rectangle((cx - dw // 2, cy + 4, cx + dw // 2, cy + h // 2), fill=BG)


def _icon_alphabet(draw, cx, cy, s, color):
    w, h = int(28 * s), int(36 * s)
    thick = max(3, int(5 * s))
    draw.line((cx - w // 2, cy + h // 2, cx, cy - h // 2), fill=color, width=thick)
    draw.line((cx + w // 2, cy + h // 2, cx, cy - h // 2), fill=color, width=thick)
    draw.line((cx - int(8 * s), cy + int(6 * s), cx + int(8 * s), cy + int(6 * s)), fill=color, width=thick)


def _icon_decade(draw, cx, cy, s, color):
    w, h = int(40 * s), int(36 * s)
    draw.rounded_rectangle((cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2), radius=6, fill=color)
    draw.rectangle((cx - w // 2, cy - h // 2, cx + w // 2, cy - h // 2 + int(10 * s)), fill=(255, 236, 180))
    for dx in (-10, 0, 10):
        _circle(draw, cx + int(dx * s), cy + int(6 * s), max(3, int(3 * s)), BG)


ICONS = {
    "bottles": _icon_bottles,
    "country": _icon_country,
    "explorer": _icon_explorer,
    "type": _icon_type,
    "grape": _icon_grape,
    "producer": _icon_producer,
    "alphabet": _icon_alphabet,
    "decade": _icon_decade,
}


def _frame(family: str, t: int) -> Image.Image:
    im = Image.new("RGB", (SIZE, SIZE), BG)
    draw = ImageDraw.Draw(im)
    color = FAMILIES[family]
    s = _scale(t)
    _spark(draw, SIZE // 2, SIZE // 2, t, color)
    ICONS[family](draw, SIZE // 2, SIZE // 2, s, color)
    # pop ring
    r = int(54 * min(1.0, s))
    draw.ellipse(
        (SIZE // 2 - r, SIZE // 2 - r, SIZE // 2 + r, SIZE // 2 + r),
        outline=color, width=2,
    )
    return im


def build(family: str, dest: Path) -> None:
    frames = [_frame(family, t) for t in range(FRAMES)]
    frames[0].save(
        dest,
        save_all=True,
        append_images=frames[1:],
        duration=90,
        loop=0,
        optimize=True,
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for family in FAMILIES:
        path = OUT / f"{family}.gif"
        build(family, path)
        print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
