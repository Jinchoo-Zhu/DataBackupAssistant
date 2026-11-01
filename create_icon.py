from pathlib import Path

from PIL import Image, ImageDraw


def rounded_rectangle(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


output = Path(__file__).resolve().parent / "assets"
output.mkdir(exist_ok=True)

size = 512
image = Image.new("RGBA", (size, size), (16, 42, 67, 255))
draw = ImageDraw.Draw(image)

rounded_rectangle(draw, (45, 45, 467, 467), 104, (23, 107, 255, 255))
draw.ellipse((126, 103, 386, 225), fill=(255, 255, 255, 255))
draw.rectangle((126, 164, 386, 322), fill=(255, 255, 255, 255))
draw.ellipse((126, 262, 386, 384), fill=(255, 255, 255, 255))
draw.rectangle((126, 236, 386, 348), fill=(226, 239, 255, 255))
draw.ellipse((126, 286, 386, 408), fill=(226, 239, 255, 255))
draw.ellipse((126, 108, 386, 230), outline=(20, 90, 220, 255), width=12)
draw.arc((118, 166, 394, 366), 0, 180, fill=(20, 90, 220, 255), width=12)

draw.arc((263, 296, 473, 464), 120, 350, fill=(93, 214, 165, 255), width=34)
draw.polygon(
    [(447, 301), (478, 349), (417, 356)],
    fill=(93, 214, 165, 255),
)

image.save(output / "app.png")
image.save(
    output / "app.ico",
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)

