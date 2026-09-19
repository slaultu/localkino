# -*- coding: utf-8 -*-
"""Generate the app icon as a PNG, using nothing but the standard library.

Original artwork: a dark rounded tile with an orange play disc, matching the
colours the app itself uses. Shapes are drawn as distance fields so the edges
come out smooth without any imaging library.
"""
import math
import struct
import sys
import zlib

SIZE = 1024
BG_TOP = (27, 36, 48)
BG_BOTTOM = (13, 16, 22)
ACCENT = (255, 122, 47)
ACCENT_LIT = (255, 163, 106)
WHITE = (255, 255, 255)


def mix(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def over(base, colour, alpha):
    return tuple(round(base[i] + (colour[i] - base[i]) * alpha) for i in range(3))


def coverage(distance, feather):
    """1 inside the shape, 0 outside, smooth across `feather` pixels."""
    return max(0.0, min(1.0, 0.5 - distance / feather))


def rounded_box(x, y, half, radius):
    dx = abs(x) - (half - radius)
    dy = abs(y) - (half - radius)
    outside = math.hypot(max(dx, 0.0), max(dy, 0.0))
    return outside + min(max(dx, dy), 0.0) - radius


def triangle(x, y, size):
    """Play glyph: distance to a rounded equilateral triangle pointing right."""
    edges = [
        (x * 0.5 + y * 0.866) - size * 0.5,      # upper edge
        (x * 0.5 - y * 0.866) - size * 0.5,      # lower edge
        -x - size * 0.5,                          # back edge
    ]
    inside = max(edges)
    return inside - size * 0.08                   # slight rounding


def render():
    centre = SIZE / 2.0
    rows = []
    for py in range(SIZE):
        row = bytearray()
        y = py - centre
        for px in range(SIZE):
            x = px - centre
            pixel = mix(BG_TOP, BG_BOTTOM, py / float(SIZE))
            alpha = coverage(rounded_box(x, y, centre, SIZE * 0.225), 2.0)
            if alpha <= 0.0:
                row += bytes((0, 0, 0, 0))
                continue

            # orange disc, a touch brighter at the top for a little depth
            disc = coverage(math.hypot(x, y) - SIZE * 0.29, 2.0)
            if disc > 0:
                shade = mix(ACCENT_LIT, ACCENT, (py / float(SIZE)) * 1.4)
                pixel = over(pixel, shade, disc)

            glyph = coverage(triangle(x - SIZE * 0.025, y, SIZE * 0.20), 2.0)
            if glyph > 0:
                pixel = over(pixel, WHITE, glyph)

            row += bytes((pixel[0], pixel[1], pixel[2], round(alpha * 255)))
        rows.append(bytes(row))
    return rows


def write_png(path, rows):
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(tag, payload):
        body = tag + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
        handle.write(chunk(b"IHDR", header))
        handle.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        handle.write(chunk(b"IEND", b""))


if __name__ == "__main__":
    destination = sys.argv[1] if len(sys.argv) > 1 else "icon.png"
    write_png(destination, render())
    print("wrote", destination)
