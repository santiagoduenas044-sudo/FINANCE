#!/usr/bin/env python3
"""
Render the 1024x1024 iOS app icon.

iOS icons must have no alpha channel and no rounded corners of their own —
the system applies its own mask — so this writes an opaque RGB PNG covering
the full square. Shapes are filled by horizontal spans with fractional edge
coverage, which keeps it fast and smooth without any imaging library.

Usage:  python3 tools/make-ios-icon.py ios/Resources/Assets.xcassets/AppIcon.appiconset/icon-1024.png
"""
import math
import struct
import sys
import zlib

SIZE = 1024
SS = 3                      # vertical supersamples per output row
SCALE = SIZE / 512.0        # the source artwork is drawn on a 512 grid

# Background gradient, top to bottom.
BG_TOP = (0x1B, 0x1D, 0x22)
BG_BOTTOM = (0x0B, 0x0C, 0x0E)

# Four buckets at increasing heights, then the rule above them.
# (x, y, w, h, radius, colour) on the 512 grid.
SHAPES = [
    (104, 296, 52, 112, 14, (0x39, 0x87, 0xE5)),   # blue
    (180, 240, 52, 168, 14, (0x19, 0x9E, 0x70)),   # aqua
    (256, 176, 52, 232, 14, (0x90, 0x85, 0xE9)),   # violet
    (332, 128, 52, 280, 14, (0xC9, 0x85, 0x00)),   # gold
    (104, 102, 132, 14, 7, (0xF5, 0xF6, 0xF8)),    # ink rule
]


def scaled(shape):
    x, y, w, h, r, colour = shape
    return (x * SCALE, y * SCALE, w * SCALE, h * SCALE, r * SCALE, colour)


def span(x, y, w, h, r, py):
    """Horizontal extent of a rounded rectangle at sample row `py`, or None."""
    if py < y or py > y + h:
        return None
    if py < y + r:
        dy = (y + r) - py
    elif py > y + h - r:
        dy = py - (y + h - r)
    else:
        return (x, x + w)
    inset = r - math.sqrt(max(0.0, r * r - dy * dy))
    return (x + inset, x + w - inset)


def render():
    shapes = [scaled(s) for s in SHAPES]
    out = bytearray()
    inv = 1.0 / SS

    for py in range(SIZE):
        acc_r = [0.0] * SIZE
        acc_g = [0.0] * SIZE
        acc_b = [0.0] * SIZE

        for sub in range(SS):
            sy = py + (sub + 0.5) * inv
            t = sy / SIZE
            br = BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t
            bg = BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t
            bb = BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t

            row_r = [br] * SIZE
            row_g = [bg] * SIZE
            row_b = [bb] * SIZE

            for (x, y, w, h, r, colour) in shapes:
                s = span(x, y, w, h, r, sy)
                if not s:
                    continue
                x0, x1 = s
                cr, cg, cb = colour
                i0, i1 = int(math.floor(x0)), int(math.floor(x1))
                i0c, i1c = max(0, i0), min(SIZE - 1, i1)

                # solid interior
                if i1c > i0c + 1:
                    n = i1c - (i0c + 1)
                    row_r[i0c + 1:i1c] = [float(cr)] * n
                    row_g[i0c + 1:i1c] = [float(cg)] * n
                    row_b[i0c + 1:i1c] = [float(cb)] * n

                # fractional edges
                for idx, cov in ((i0, 1.0 - (x0 - i0)), (i1, x1 - i1)):
                    if 0 <= idx < SIZE and cov > 0:
                        k = min(1.0, cov)
                        row_r[idx] += (cr - row_r[idx]) * k
                        row_g[idx] += (cg - row_g[idx]) * k
                        row_b[idx] += (cb - row_b[idx]) * k

            for i in range(SIZE):
                acc_r[i] += row_r[i]
                acc_g[i] += row_g[i]
                acc_b[i] += row_b[i]

        out.append(0)  # PNG filter byte: none
        for i in range(SIZE):
            out.append(min(255, max(0, int(acc_r[i] * inv + 0.5))))
            out.append(min(255, max(0, int(acc_g[i] * inv + 0.5))))
            out.append(min(255, max(0, int(acc_b[i] * inv + 0.5))))
    return bytes(out)


def write_png(path, raw):
    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack('>IIBBBBB', SIZE, SIZE, 8, 2, 0, 0, 0)  # colour type 2 = RGB, no alpha
    blob = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))
    with open(path, 'wb') as fh:
        fh.write(blob)
    return len(blob)


if __name__ == '__main__':
    target = sys.argv[1] if len(sys.argv) > 1 else 'icon-1024.png'
    size = write_png(target, render())
    print(f'{target} — {size // 1024} KB')
