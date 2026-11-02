from pathlib import Path

from PIL import Image


ASSETS = Path(__file__).resolve().parent / "assets"
SOURCE = ASSETS / "app-source.png"
PNG_OUTPUT = ASSETS / "app.png"
HEADER_OUTPUT = ASSETS / "app-48.png"
ICO_OUTPUT = ASSETS / "app.ico"


def _prepare_icon(image: Image.Image) -> Image.Image:
    bbox = image.getchannel("A").getbbox()
    if bbox is not None:
        image = image.crop(bbox)

    # Keep the original aspect ratio, then center it in the same 256 canvas.
    side = max(image.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.alpha_composite(
        image,
        ((side - image.width) // 2, (side - image.height) // 2),
    )

    canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    canvas.alpha_composite(
        square,
        ((256 - square.width) // 2, (256 - square.height) // 2),
    )
    return canvas


def main() -> None:
    if not SOURCE.is_file():
        raise FileNotFoundError(f"找不到图标源文件：{SOURCE}")

    with Image.open(SOURCE) as source:
        image = source.convert("RGBA")

    icon = _prepare_icon(image)
    icon.save(PNG_OUTPUT)
    icon.resize((48, 48), Image.Resampling.LANCZOS).save(HEADER_OUTPUT)

    icon.save(
        ICO_OUTPUT,
        format="ICO",
        sizes=[
            (16, 16),
            (20, 20),
            (24, 24),
            (32, 32),
            (40, 40),
            (48, 48),
            (64, 64),
            (128, 128),
            (256, 256),
        ],
    )


if __name__ == "__main__":
    main()

