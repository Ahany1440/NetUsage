"""Draws netusage.ico: a cyan download arrow and an amber upload arrow, the app's two colours.
Run: python make_icon.py"""
from PIL import Image, ImageDraw

S = 1024  # draw big, scale down for each icon size
BG, DOWN, UP = "#1c2129", "#4cc3d9", "#e8a54b"


def arrow(d, x, top, bottom, color, pointing_down):
    shaft, head = 104, 160  # shaft width, half the arrowhead width
    if pointing_down:
        d.rounded_rectangle((x - shaft // 2, top, x + shaft // 2, bottom - 200), radius=shaft // 2, fill=color)
        d.polygon([(x - head, bottom - 260), (x + head, bottom - 260), (x, bottom)], fill=color)
    else:
        d.rounded_rectangle((x - shaft // 2, top + 200, x + shaft // 2, bottom), radius=shaft // 2, fill=color)
        d.polygon([(x - head, top + 260), (x + head, top + 260), (x, top)], fill=color)


img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((32, 32, S - 32, S - 32), radius=220, fill=BG)
arrow(d, 360, 200, 830, DOWN, pointing_down=True)
arrow(d, 664, 194, 824, UP, pointing_down=False)
img.save("netusage.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)])
img.resize((256, 256), Image.LANCZOS).save("netusage_preview.png")
print("saved netusage.ico")
