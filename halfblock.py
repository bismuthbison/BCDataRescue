"""Draw a picture as text, in Apple ][ phosphor green. For bcdata_tui's news reader.

Each text cell shows two stacked "pixels" using the upper-half-block character: the foreground colour is the
top pixel and the background colour is the bottom pixel. A terminal cell is about twice as tall as it is
wide, so two pixels per cell comes out roughly square. It works in any terminal with 24-bit colour, with no
special graphics support, and the result is a chunky thumbnail rather than a sharp picture.

The picture is converted to a grayscale brightness and then tinted green, to match the rest of the app.
"""
import io

from PIL import Image, ImageOps
from rich.text import Text

# Phosphor green as RGB, the same colour as the app theme (#33ff33). Brightness scales it from black.
GREEN = (0x33, 0xFF, 0x33)
MAX_SOURCE_PIXELS = 40_000_000  # refuse absurdly large images instead of decoding them (decompression bombs)


def render_green(data: bytes, max_cols: int = 60, max_rows: int = 24) -> Text:
    """Turn image bytes (JPEG, PNG, ...) into green half-block text.

    The picture keeps its proportions and is made as large as fits in max_cols columns by max_rows text rows.
    Raises ValueError if the data isn't a usable image (the caller should treat that as "no picture").
    """
    try:
        im = Image.open(io.BytesIO(data))
        if im.width * im.height > MAX_SOURCE_PIXELS:
            raise ValueError("image is too large")
        # For JPEGs this decodes at reduced size, which is much faster for a big photo.
        im.draft("L", (max_cols * 4, max_rows * 8))
        im = im.convert("L")  # brightness only; the colour comes from GREEN
    except ValueError:
        raise
    except Exception as e:  # Pillow raises several different types for bad or truncated files
        raise ValueError(f"couldn't read the image ({e})") from e

    # Size in "pixels": columns across, and two pixel rows per text row. Fit inside max_cols x (2 * max_rows).
    scale = min(max_cols / im.width, (2 * max_rows) / im.height)
    cols = max(1, int(im.width * scale))
    pixel_rows = max(2, int(im.height * scale))
    pixel_rows -= pixel_rows % 2  # whole text rows only
    im = ImageOps.autocontrast(im.resize((cols, pixel_rows), Image.LANCZOS), cutoff=1)  # use the full dark-to-light range

    def tint(level: int) -> str:
        """Brightness 0-255 -> a green on the phosphor scale, as a hex colour."""
        f = level / 255
        return "#%02x%02x%02x" % (int(GREEN[0] * f), int(GREEN[1] * f), int(GREEN[2] * f))

    text = Text(no_wrap=True)
    for y in range(0, pixel_rows, 2):
        for x in range(cols):
            text.append("▀", style=f"{tint(im.getpixel((x, y)))} on {tint(im.getpixel((x, y + 1)))}")
        text.append("\n")
    return text
