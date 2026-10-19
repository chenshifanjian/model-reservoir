"""生成应用图标：assets/icon.png / icon.ico / icon.icns

纯 PIL 绘制，无外部素材依赖。设计意图（不依赖 emoji）：
深青圆角方块 + 白色水滴 + 两层水位线 —— 「蓄水池」。

用法：.venv-gui/bin/python scripts/make_icon.py
"""
import math
import struct
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets"

BG_TOP = (13, 116, 110)
BG_BOTTOM = (8, 47, 63)
WATER_BACK = (34, 211, 238)
WATER_FRONT = (6, 148, 184)
EDGE = (94, 234, 212)


def _gradient(size):
    img = Image.new("RGB", (1, size))
    px = img.load()
    for y in range(size):
        t = y / max(1, size - 1)
        # 轻微加速曲线，让上半部更亮
        t = t ** 1.15
        px[0, y] = tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3))
    return img.resize((size, size), Image.BICUBIC)


def _wave_mask(y_base, amplitude, period, phase=0.0):
    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)
    pts = []
    for x in range(0, S + 8, 8):
        y = y_base + amplitude * math.sin(2 * math.pi * x / period + phase)
        pts.append((x, y))
    pts += [(S + 8, S), (-8, S)]
    d.polygon(pts, fill=255)
    return m


def build():
    img = _gradient(S).convert("RGBA")

    # 水滴
    drop = Image.new("L", (S, S), 0)
    dd = ImageDraw.Draw(drop)
    cx, cy, r = 512, 486, 104
    dd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    dd.polygon([(cx, 286), (cx - r + 8, cy + 6), (cx + r - 8, cy + 6)], fill=255)
    white = Image.new("RGBA", (S, S), (255, 255, 255, 0))
    white.putalpha(drop)
    white_layer = Image.new("RGBA", (S, S), (255, 255, 255, 255))
    img.paste(white_layer, (0, 0), drop)

    # 水位：后层浅、前层深，两条相位不同的正弦波
    back = _wave_mask(636, 20, 420, 0.0)
    front = _wave_mask(724, 16, 300, 2.4)
    img.paste(Image.new("RGBA", (S, S), WATER_BACK + (232,)), (0, 0), back)
    img.paste(Image.new("RGBA", (S, S), WATER_FRONT + (255,)), (0, 0), front)

    # 水滴高光：左下角的浅色弧，让白色不至于死板
    hl = Image.new("RGBA", (S, S), (255, 255, 255, 0))
    hd = ImageDraw.Draw(hl)
    hd.ellipse((cx - 52, cy - 34, cx - 6, cy + 40), fill=(255, 255, 255, 120))
    img.alpha_composite(hl)

    # 圆角裁切
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=218, fill=255)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)

    # 内描边：深色任务栏/浅色任务栏下都能看出边界
    ring = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(ring).rounded_rectangle((10, 10, S - 11, S - 11), radius=208,
                                           outline=EDGE + (70,), width=10)
    out.alpha_composite(ring)
    return out


def write_ico(img, path):
    img.save(path, format="ICO",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def write_icns(img, path):
    """手写 icns 容器：PIL 的 ICNS 写入对小尺寸支持不稳，这里自己封。"""
    entries = [
        (b"icp4", 16), (b"icp5", 32), (b"icp6", 64),
        (b"ic07", 128), (b"ic08", 256), (b"ic09", 512), (b"ic10", 1024),
    ]
    import io
    body = b""
    for code, size in entries:
        buf = io.BytesIO()
        img.resize((size, size), Image.LANCZOS).save(buf, format="PNG")
        data = buf.getvalue()
        body += code + struct.pack(">I", len(data) + 8) + data
    path.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)


def main():
    OUT.mkdir(exist_ok=True)
    img = build()
    img.save(OUT / "icon.png")
    write_ico(img, OUT / "icon.ico")
    write_icns(img, OUT / "icon.icns")
    # 顺带给 Linux 桌面项用的 512 PNG
    img.resize((512, 512), Image.LANCZOS).save(OUT / "icon-512.png")
    for p in ("icon.png", "icon.ico", "icon.icns", "icon-512.png"):
        f = OUT / p
        print(f"{p:14} {f.stat().st_size:>8} bytes")


if __name__ == "__main__":
    main()
