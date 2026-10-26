"""Draws Marquee's icon (assets/marquee.ico + .png): a Clear Home navy tile with a play mark and the orange
marquee bulbs of a cinema sign."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "assets"
S = 1024


def draw():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # tile with a top-left sheen
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / S
        gd.line([(0, y), (S, y)], fill=(int(0x2f + (0x14 - 0x2f) * t), int(0x55 + (0x20 - 0x55) * t), int(0xb5 + (0x3f - 0xb5) * t), 255))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([40, 40, S - 40, S - 40], radius=230, fill=255)
    img.paste(grad, (0, 0), mask)
    sheen = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(sheen).ellipse([-200, -420, 760, 430], fill=(255, 255, 255, 46))
    img = Image.alpha_composite(img, Image.composite(sheen, Image.new("RGBA", (S, S)), mask))
    d = ImageDraw.Draw(img)
    # screen
    d.rounded_rectangle([230, 300, 794, 690], radius=70, fill=(255, 255, 255, 255))
    d.polygon([(450, 395), (450, 595), (620, 495)], fill=(0x23, 0x40, 0x8f, 255))
    # marquee bulbs along the bottom
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    for i in range(5):
        x = 300 + i * 106
        gdraw.ellipse([x - 44, 760 - 44, x + 44, 760 + 44], fill=(0xff, 0x8a, 0x1f, 150))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(18)))
    d = ImageDraw.Draw(img)
    for i in range(5):
        x = 300 + i * 106
        d.ellipse([x - 26, 760 - 26, x + 26, 760 + 26], fill=(0xff, 0x8a, 0x1f, 255), outline=(255, 255, 255, 255), width=8)
    return img


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    img = draw()
    img.save(OUT / "marquee.png")
    img.save(OUT / "marquee.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", OUT / "marquee.ico")
