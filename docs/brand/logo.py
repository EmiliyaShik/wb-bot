# -*- coding: utf-8 -*-
"""Логотип: буква Р со стрелкой роста. Буква построена по осям, а не на глаз.

Стойка и чаша буквы согласованы: верх чаши ровно на верхнем срезе стойки,
толщина обводки чаши равна ширине стойки, низ чаши ровно на середине буквы.
"""
import math, struct, zlib

S, K = 512, 3
W = S * K

BG_TOP, BG_BOT = (0.043, 0.106, 0.180), (0.031, 0.071, 0.125)
GLOW = (0.063, 0.282, 0.353)
TEAL = (0.357, 0.882, 0.816)
WHITE = (1.0, 1.0, 1.0)

# --- буква ---
SX = -0.14          # ось стойки
TOP, BOT = -0.42, 0.42
HALF = 0.052        # половина толщины линии
BOWL_BOT = 0.06     # низ чаши чуть ниже середины, как в наборных шрифтах
# Полуоси задают середину линии, а не внешний край, поэтому из них вычтена
# половина толщины: иначе чаша торчала бы над стойкой на эту половину, и у
# верхнего среза буквы получалась ступенька.
BOWL_CX = SX + HALF            # вершина чаши ровно у правого края стойки
BOWL_CY = (TOP + BOWL_BOT) / 2
A_OUT, B_OUT = 0.27, (BOWL_BOT - TOP) / 2      # внешний край чаши
A_IN, B_IN = A_OUT - 2 * HALF, B_OUT - 2 * HALF  # внутренний просвет


def mix(a, b, t):
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def over(dst, src, a):
    return tuple(d + (s - d) * a for d, s in zip(dst, src))


def rrect(px, py, hw, hh, r):
    dx, dy = abs(px) - hw + r, abs(py) - hh + r
    return math.hypot(max(dx, 0.0), max(dy, 0.0)) + min(max(dx, dy), 0.0) - r


def seg(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    t = (wx * vx + wy * vy) / (vx * vx + vy * vy)
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return math.hypot(wx - vx * t, wy - vy * t)


def letter(px, py):
    """Внутри буквы или нет. Знак минус значит внутри.

    Чаша это кольцо между двумя овалами, а не обводка одного. Приближённое
    расстояние до овала у вершины врёт в большую сторону, и линия там
    раздувалась горбом: на верхнем срезе буквы это читалось как брак.
    """
    if rrect(px - SX, py - (TOP + BOT) / 2, HALF, (BOT - TOP) / 2, 0.004) < 0:
        return -1.0
    if px < BOWL_CX:
        return 1.0
    ux, uy = (px - BOWL_CX) / A_OUT, (py - BOWL_CY) / B_OUT
    if ux * ux + uy * uy > 1.0:
        return 1.0
    vx, vy = (px - BOWL_CX) / A_IN, (py - BOWL_CY) / B_IN
    return 1.0 if vx * vx + vy * vy < 1.0 else -1.0


def scene(px, py):
    col = mix(BG_TOP, BG_BOT, (py + 1) / 2)
    col = over(col, GLOW, max(0.0, 1 - math.hypot(px, py) * 1.3) ** 2 * 0.6)
    col = over(col, TEAL, 1.0 if abs(math.hypot(px, py) - 0.72) - 0.028 < 0 else 0.0)
    col = over(col, WHITE, 1.0 if letter(px, py) < 0 else 0.0)
    arrow = min(
        seg(px, py, 0.16, 0.34, 0.42, 0.08),
        seg(px, py, 0.42, 0.08, 0.42, 0.24),
        seg(px, py, 0.42, 0.08, 0.26, 0.08),
    )
    col = over(col, TEAL, 1.0 if arrow - 0.030 < 0 else 0.0)
    return col


def render(path):
    half = W / 2.0
    rows = []
    for yy in range(W):
        py = (yy - half) / half
        row = bytearray()
        for xx in range(W):
            col = scene((xx - half) / half, py)
            row += bytes(int(max(0, min(255, round(c * 255)))) for c in col)
        rows.append(bytes(row))
    out = bytearray()
    for y in range(S):
        out.append(0)
        for x in range(S):
            r = g = b = 0
            for dy in range(K):
                line = rows[y * K + dy]
                base = x * K * 3
                for dx in range(K):
                    r += line[base + dx * 3]
                    g += line[base + dx * 3 + 1]
                    b += line[base + dx * 3 + 2]
            n = K * K
            out += bytes((r // n, g // n, b // n))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(
            ">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", S, S, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(out), 9))
    png += chunk(b"IEND", b"")
    open(path, "wb").write(png)
    print("готово:", path)


render("logo-r.png")
