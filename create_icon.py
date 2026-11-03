import io
import struct
from pathlib import Path

from PIL import Image

ASSETS = Path(__file__).resolve().parent / "assets"
SOURCE = ASSETS / "app-source.png"
PNG_OUTPUT = ASSETS / "app.png"
HEADER_OUTPUT = ASSETS / "app-48.png"
ICO_OUTPUT = ASSETS / "app.ico"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
OPTIMISED_FRAMES = {
    24: ASSETS / "图标24×24.png",
    32: ASSETS / "图标32×32.png",
}


def _normalise_canvas(image: Image.Image) -> Image.Image:
    """Remove transparent outer padding while preserving the icon aspect ratio."""
    bbox = image.getchannel("A").getbbox()
    if bbox is not None:
        image = image.crop(bbox)

    side = max(image.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.alpha_composite(
        image,
        ((side - image.width) // 2, (side - image.height) // 2),
    )
    return square.resize((256, 256), Image.Resampling.LANCZOS)


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _write_ico(
    path: Path,
    source: Image.Image,
    overrides: dict[int, Path],
) -> None:
    frames: list[tuple[int, bytes]] = []
    for size in SIZES:
        override = overrides.get(size)
        if override is not None and override.is_file():
            with Image.open(override) as frame:
                actual = frame.size
                mode = frame.mode
            if actual != (size, size):
                raise ValueError(
                    f"{override.name} 实际尺寸为 {actual}，预期为 {size}×{size}"
                )
            if mode != "RGBA":
                raise ValueError(f"{override.name} 必须是 RGBA PNG")
            payload = override.read_bytes()
        else:
            payload = _png_bytes(
                source.resize((size, size), Image.Resampling.LANCZOS)
            )
        frames.append((size, payload))

    offset = 6 + 16 * len(frames)
    header = struct.pack("<HHH", 0, 1, len(frames))
    directory = bytearray()
    for size, payload in frames:
        dimension = 0 if size == 256 else size
        directory.extend(
            struct.pack(
                "<BBBBHHII",
                dimension,
                dimension,
                0,
                0,
                1,
                32,
                len(payload),
                offset,
            )
        )
        offset += len(payload)
    path.write_bytes(
        header
        + directory
        + b"".join(payload for _, payload in frames)
    )


def main() -> None:
    if not SOURCE.is_file():
        raise FileNotFoundError(f"找不到图标源文件：{SOURCE}")

    with Image.open(SOURCE) as source:
        image = _normalise_canvas(source.convert("RGBA"))

    # Use the 24/32 taskbar images exactly as supplied; derive other frames
    # from the normalized 256 image with the same LANCZOS pipeline as AnhuSimMate.
    image.save(PNG_OUTPUT)
    image.resize((48, 48), Image.Resampling.LANCZOS).save(HEADER_OUTPUT)
    _write_ico(ICO_OUTPUT, image, OPTIMISED_FRAMES)


if __name__ == "__main__":
    main()
