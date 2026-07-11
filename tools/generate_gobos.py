# SPDX-License-Identifier: CC0-1.0
"""Build-time gobo texture generator (no dependencies, stdlib only).

Writes ~20 grayscale PNGs into ../data/gobos/. White = light passes,
black = blocked. All output is procedurally generated and dedicated to the
public domain (CC0-1.0).

Run:  python tools/generate_gobos.py
"""

import math
import os
import random
import struct
import zlib

SIZE = 512
OUT_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "data", "gobos"))


def write_png(path, pixels, size=SIZE):
    """Minimal 8-bit grayscale PNG writer."""
    raw = b"".join(
        b"\x00" + bytes(pixels[y * size:(y + 1) * size])
        for y in range(size))
    def chunk(tag, data):
        block = tag + data
        return (struct.pack(">I", len(data)) + block
                + struct.pack(">I", zlib.crc32(block)))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", ihdr))
        fh.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        fh.write(chunk(b"IEND", b""))


class Canvas:
    def __init__(self, fill=0):
        self.px = bytearray([fill]) * (SIZE * SIZE)

    def rect(self, x0, y0, x1, y1, value):
        x0, x1 = max(0, int(x0)), min(SIZE, int(x1))
        y0, y1 = max(0, int(y0)), min(SIZE, int(y1))
        for y in range(y0, y1):
            row = y * SIZE
            for x in range(x0, x1):
                self.px[row + x] = value

    def circle(self, cx, cy, r, value):
        r2 = r * r
        for y in range(max(0, int(cy - r)), min(SIZE, int(cy + r + 1))):
            row = y * SIZE
            dy2 = (y - cy) ** 2
            for x in range(max(0, int(cx - r)), min(SIZE, int(cx + r + 1))):
                if (x - cx) ** 2 + dy2 <= r2:
                    self.px[row + x] = value

    def line(self, x0, y0, x1, y1, width, value):
        steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
        for i in range(steps):
            t = i / max(steps - 1, 1)
            self.circle(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t,
                        width / 2.0, value)

    def blur(self, radius, passes=2):
        """Separable box blur — soft gobo edges read as distance from the
        light's focal plane."""
        for _ in range(passes):
            for horizontal in (True, False):
                src = bytes(self.px)
                w = radius
                norm = 2 * w + 1
                for y in range(SIZE):
                    for x in range(SIZE):
                        acc = 0
                        for d in range(-w, w + 1):
                            if horizontal:
                                xx = min(max(x + d, 0), SIZE - 1)
                                acc += src[y * SIZE + xx]
                            else:
                                yy = min(max(y + d, 0), SIZE - 1)
                                acc += src[yy * SIZE + x]
                        self.px[y * SIZE + x] = acc // norm


# ------------------------------------------------------------- pattern lib
def window_panes(cols, rows, frame=26, mullion=16, margin=40):
    c = Canvas(0)
    c.rect(margin, margin, SIZE - margin, SIZE - margin, 255)
    inner_w = SIZE - 2 * margin
    for i in range(1, cols):
        x = margin + inner_w * i / cols
        c.rect(x - mullion / 2, margin, x + mullion / 2, SIZE - margin, 0)
    for j in range(1, rows):
        y = margin + inner_w * j / rows
        c.rect(margin, y - mullion / 2, SIZE - margin, y + mullion / 2, 0)
    c.rect(margin - frame, margin - frame, SIZE - margin + frame, margin, 0)
    c.rect(margin - frame, SIZE - margin, SIZE - margin + frame,
           SIZE - margin + frame, 0)
    c.rect(margin - frame, margin, margin, SIZE - margin, 0)
    c.rect(SIZE - margin, margin, SIZE - margin + frame, SIZE - margin, 0)
    return c


def window_arched():
    c = Canvas(0)
    margin, spring = 60, 240
    c.rect(margin, spring, SIZE - margin, SIZE - margin, 255)
    cx, r = SIZE / 2, SIZE / 2 - margin
    for y in range(int(spring - r), spring):
        row = y * SIZE
        dy = spring - y
        half = math.sqrt(max(r * r - dy * dy, 0))
        for x in range(int(cx - half), int(cx + half)):
            c.px[row + x] = 255
    c.rect(cx - 9, spring - r - 4, cx + 9, SIZE - margin, 0)   # centre bar
    c.rect(margin, spring - 9, SIZE - margin, spring + 9, 0)   # transom
    c.rect(margin, (spring + SIZE - margin) / 2 - 8,
           SIZE - margin, (spring + SIZE - margin) / 2 + 8, 0)
    return c


def blinds(slats, gap_ratio=0.55, jitter=0.0, rng=None):
    c = Canvas(0)
    pitch = SIZE / slats
    for i in range(slats):
        y0 = i * pitch
        off = (rng.uniform(-jitter, jitter) * pitch) if rng else 0.0
        c.rect(0, y0 + off, SIZE, y0 + pitch * gap_ratio + off, 255)
    return c


def diagonal_stripes(stripes=14, gap_ratio=0.5):
    c = Canvas(0)
    pitch = SIZE * 2 / stripes
    for y in range(SIZE):
        row = y * SIZE
        for x in range(SIZE):
            if ((x + y) % pitch) < pitch * gap_ratio:
                c.px[row + x] = 255
    return c


def foliage(rng, clusters, leaves_per, leaf_r):
    c = Canvas(255)  # open sky, dark leaf clusters
    for _ in range(clusters):
        cx = rng.uniform(0, SIZE)
        cy = rng.uniform(0, SIZE)
        spread = rng.uniform(40, 110)
        for _ in range(leaves_per):
            a = rng.uniform(0, math.tau)
            d = abs(rng.gauss(0, spread / 2))
            r = rng.uniform(leaf_r * 0.5, leaf_r * 1.5)
            c.circle(cx + math.cos(a) * d, cy + math.sin(a) * d, r, 0)
    return c


def branches(rng, count=7):
    c = Canvas(255)
    for _ in range(count):
        x = rng.uniform(0, SIZE)
        y = rng.choice([0.0, SIZE])
        angle = rng.uniform(-0.6, 0.6) + (math.pi / 2 if y == 0
                                          else -math.pi / 2)
        width = rng.uniform(14, 26)
        length = 0.0
        while 0 <= x < SIZE and 0 <= y < SIZE + 1 and length < SIZE * 1.4:
            step = rng.uniform(18, 34)
            nx = x + math.cos(angle) * step
            ny = y + math.sin(angle) * step
            c.line(x, y, nx, ny, width, 0)
            if rng.random() < 0.35 and width > 6:
                twig_a = angle + rng.uniform(-1.1, 1.1)
                tx = x + math.cos(twig_a) * step * 3
                ty = y + math.sin(twig_a) * step * 3
                c.line(x, y, tx, ty, width * 0.4, 0)
            angle += rng.uniform(-0.35, 0.35)
            width *= rng.uniform(0.88, 0.98)
            x, y, length = nx, ny, length + step
    return c


def cucoloris(rng, holes, r_lo, r_hi, organic=True):
    c = Canvas(0)  # solid cuc board with cut-outs
    for _ in range(holes):
        cx, cy = rng.uniform(0, SIZE), rng.uniform(0, SIZE)
        if organic:
            base = rng.uniform(r_lo, r_hi)
            for _ in range(rng.randint(3, 6)):  # blobby unions
                c.circle(cx + rng.uniform(-base, base) * 0.7,
                         cy + rng.uniform(-base, base) * 0.7,
                         base * rng.uniform(0.5, 1.0), 255)
        else:
            w = rng.uniform(r_lo, r_hi)
            h = rng.uniform(r_lo, r_hi)
            c.rect(cx - w, cy - h, cx + w, cy + h, 255)
    return c


def grid(bars_x=6, bars_y=6, bar=14):
    c = Canvas(255)
    for i in range(bars_x + 1):
        x = SIZE * i / bars_x
        c.rect(x - bar / 2, 0, x + bar / 2, SIZE, 0)
    for j in range(bars_y + 1):
        y = SIZE * j / bars_y
        c.rect(0, y - bar / 2, SIZE, y + bar / 2, 0)
    return c


def chainlink(cells=9, wire=7):
    c = Canvas(255)
    pitch = SIZE / cells
    for i in range(-1, cells + 1):
        off = i * pitch
        c.line(off, 0, off + SIZE, SIZE, wire, 0)
        c.line(off + SIZE, 0, off, SIZE, wire, 0)
        c.line(off - SIZE, 0, off, SIZE, wire, 0)
        c.line(off, 0, off - SIZE, SIZE, wire, 0)
    return c


def stair_rail():
    c = Canvas(255)
    for i in range(9):
        x = 40 + i * 54
        c.rect(x, 0, x + 16, SIZE, 0)
    c.line(0, 90, SIZE, 300, 40, 0)  # handrail diagonal
    return c


def curtain_slit():
    c = Canvas(0)
    c.rect(SIZE * 0.42, 0, SIZE * 0.58, SIZE, 255)
    c.rect(SIZE * 0.12, 0, SIZE * 0.17, SIZE, 140)
    c.rect(SIZE * 0.83, 0, SIZE * 0.88, SIZE, 140)
    return c


def dapple(rng, holes=90):
    c = Canvas(0)
    for _ in range(holes):
        c.circle(rng.uniform(0, SIZE), rng.uniform(0, SIZE),
                 rng.uniform(8, 30), 255)
    return c


def build_all():
    rng = random.Random(20260707)
    jobs = {
        "window_4_pane": lambda: window_panes(2, 2),
        "window_6_pane": lambda: window_panes(3, 2),
        "window_slim": lambda: window_panes(1, 3, margin=110),
        "window_arched": window_arched,
        "french_doors": lambda: window_panes(4, 5, mullion=10, margin=30),
        "blinds_wide": lambda: blinds(7),
        "blinds_narrow": lambda: blinds(18),
        "blinds_broken": lambda: blinds(11, jitter=0.25, rng=rng),
        "venetian_angled": lambda: diagonal_stripes(),
        "foliage_soft": lambda: foliage(rng, 10, 60, 16),
        "foliage_dense": lambda: foliage(rng, 18, 90, 14),
        "leaves_large": lambda: foliage(rng, 6, 25, 34),
        "branches_bare": lambda: branches(rng),
        "cucoloris_organic": lambda: cucoloris(rng, 26, 20, 52),
        "cucoloris_cutout": lambda: cucoloris(rng, 22, 16, 60,
                                              organic=False),
        "dapple_holes": lambda: dapple(rng),
        "grid_industrial": lambda: grid(),
        "fence_chainlink": lambda: chainlink(),
        "stair_rail": stair_rail,
        "curtain_slit": curtain_slit,
    }
    blur_amount = {
        "foliage_soft": (4, 2), "foliage_dense": (3, 2),
        "leaves_large": (5, 2), "dapple_holes": (5, 2),
        "cucoloris_organic": (4, 2), "curtain_slit": (6, 2),
        "blinds_broken": (2, 1),
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    for name, fn in jobs.items():
        canvas = fn()
        radius, passes = blur_amount.get(name, (2, 1))
        canvas.blur(radius, passes)
        path = os.path.join(OUT_DIR, f"{name}.png")
        write_png(path, canvas.px)
        print(f"  wrote {path}")
    print(f"{len(jobs)} gobos generated (CC0-1.0).")


if __name__ == "__main__":
    build_all()
