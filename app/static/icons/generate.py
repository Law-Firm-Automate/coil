"""Generate the PWA icons in code. Run: .venv/bin/python app/static/icons/generate.py
Writes icon-192.png, icon-512.png and icon-maskable-512.png next to this file with Pillow; always writes icon.svg."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
BG = (31, 95, 139)  # --accent #1f5f8b
FG = (255, 255, 255)

SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="96" fill="#1f5f8b"/>
<circle cx="256" cy="256" r="150" fill="none" stroke="#fff" stroke-width="44"/><circle cx="256" cy="256" r="72" fill="none" stroke="#fff" stroke-width="30"/>
<rect x="240" y="60" width="32" height="90" rx="10" fill="#fff"/></svg>
"""


def draw(size, maskable=False):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), BG if maskable else (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if not maskable:
        d.rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * 0.19), fill=BG)
    s = size / 512
    inset = 0.1 if maskable else 0.0  # keep the mark inside the maskable safe zone
    def sc(v):
        return int((v * (1 - 2 * inset) + 512 * inset) * s)
    w1, w2 = int(44 * s * (1 - 2 * inset)), int(30 * s * (1 - 2 * inset))
    d.ellipse((sc(106), sc(106), sc(406), sc(406)), outline=FG, width=max(w1, 2))
    d.ellipse((sc(184), sc(184), sc(328), sc(328)), outline=FG, width=max(w2, 2))
    d.rounded_rectangle((sc(240), sc(60), sc(272), sc(150)), radius=max(int(10 * s), 2), fill=FG)
    return img


def main():
    with open(os.path.join(HERE, "icon.svg"), "w") as f:
        f.write(SVG)
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print("Pillow not installed; wrote icon.svg only")
        return
    draw(192).save(os.path.join(HERE, "icon-192.png"))
    draw(512).save(os.path.join(HERE, "icon-512.png"))
    draw(512, maskable=True).convert("RGB").save(os.path.join(HERE, "icon-maskable-512.png"))
    print("wrote icon-192.png, icon-512.png, icon-maskable-512.png, icon.svg")


if __name__ == "__main__":
    main()
