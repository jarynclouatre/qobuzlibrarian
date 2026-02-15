#!/usr/bin/env python3
"""Generate the Qobuz Librarian logo as PNG assets.

Run:  python scripts/make_logo.py
Outputs (committed, so the tool ships with its branding):
  assets/logo.png                       full horizontal lockup (README)
  src/qobuz_librarian/web/static/logo.png   same lockup (web UI navbar)
  src/qobuz_librarian/web/static/icon.png   icon only (favicon)

The logo is rendered at 4x and downsampled for clean antialiasing.
Requires Pillow (dev-only; not a runtime dependency).
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
UBUNTU_B = "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf"
UBUNTU_R = "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf"

# Palette — matches the web UI's dark "night" theme.
TEAL_TOP = (13, 148, 136)     # #0D9488
CYAN_BOT = (8, 145, 178)      # #0891B2
DISC = (11, 17, 32)           # #0B1120 near-black vinyl
LABEL = (94, 234, 212)        # #5EEAD4 accent
GROOVE = (255, 255, 255, 60)  # faint white groove rings
# Wordmark colors are tuned for a LIGHT background (GitHub README).
INK = (15, 42, 46)            # #0F2A2E dark ink — "Qobuz"
SUB = (13, 148, 136)          # #0D9488 brand teal — "Librarian"
MUTED = (91, 123, 120)        # #5B7B78 tagline

SS = 4  # supersample factor


def _rounded_tile(size, radius):
    """Vertical teal→cyan gradient clipped to a rounded square."""
    grad = Image.new("RGB", (size, size))
    px = grad.load()
    for y in range(size):
        t = y / (size - 1)
        px_color = tuple(
            round(TEAL_TOP[i] + (CYAN_BOT[i] - TEAL_TOP[i]) * t) for i in range(3)
        )
        for x in range(size):
            px[x, y] = px_color
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=radius, fill=255
    )
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    tile.paste(grad, (0, 0), mask)
    return tile


def _vinyl(tile):
    """Draw a vinyl record + bookmark ribbon onto the tile (in place)."""
    d = ImageDraw.Draw(tile, "RGBA")
    s = tile.size[0]
    cx, cy = s // 2, int(s * 0.46)
    r = int(s * 0.30)

    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=DISC)
    for gr in (0.80, 0.62, 0.44):
        rr = int(r * gr)
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                  outline=GROOVE, width=max(1, s // 220))
    lr = int(r * 0.30)
    d.ellipse([cx - lr, cy - lr, cx + lr, cy + lr], fill=LABEL)
    hr = max(2, int(r * 0.06))
    d.ellipse([cx - hr, cy - hr, cx + hr, cy + hr], fill=(11, 17, 32))

    # Bookmark ribbon — the "librarian" nod.
    bw = int(s * 0.085)
    bx = cx + int(r * 0.55)
    top = int(s * 0.30)
    bot = int(s * 0.86)
    notch = int(bw * 0.55)
    d.polygon(
        [(bx, top), (bx + bw, top), (bx + bw, bot),
         (bx + bw // 2, bot - notch), (bx, bot)],
        fill=(236, 254, 255, 235),
    )
    return tile


def build():
    scale = 320 * SS
    radius = int(scale * 0.22)
    tile = _rounded_tile(scale, radius)
    _vinyl(tile)

    pad = 40 * SS
    icon_box = scale
    title_f = ImageFont.truetype(UBUNTU_B, 150 * SS)
    sub_f = ImageFont.truetype(UBUNTU_R, 52 * SS)

    canvas_h = icon_box + pad * 2
    text_x = pad + icon_box + 56 * SS
    # Measure widest text line for canvas width.
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    w1 = tmp.textlength("Qobuz Librarian", font=title_f)
    w2 = tmp.textlength("lossless library, kept tidy", font=sub_f)
    canvas_w = int(text_x + max(w1, w2) + pad)

    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    img.paste(tile, (pad, pad), tile)
    d = ImageDraw.Draw(img)

    ty = pad + 70 * SS
    d.text((text_x, ty), "Qobuz ", font=title_f, fill=INK)
    qw = d.textlength("Qobuz ", font=title_f)
    d.text((text_x + qw, ty), "Librarian", font=title_f, fill=SUB)
    d.text((text_x, ty + 168 * SS), "lossless library, kept tidy",
           font=sub_f, fill=MUTED)

    def save(im, path, w):
        path.parent.mkdir(parents=True, exist_ok=True)
        h = round(im.size[1] * w / im.size[0])
        im.resize((w, h), Image.LANCZOS).save(path)
        print(f"wrote {path.relative_to(ROOT)} ({w}x{h})")

    save(img, ROOT / "assets" / "logo.png", 1100)
    save(img, ROOT / "src/qobuz_librarian/web/static/logo.png", 880)

    icon = tile.resize((512, 512), Image.LANCZOS)
    icon.save(ROOT / "src/qobuz_librarian/web/static/icon.png")
    print("wrote src/qobuz_librarian/web/static/icon.png (512x512)")


if __name__ == "__main__":
    build()
