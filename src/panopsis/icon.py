"""panopsis's icon: the wall, four panes on violet, one of them lit because it needs you. It's
drawn here for each size it's shown at, rather than kept as an image, so even the smallest is
four panes and not a blur. In Windows, the window panopsis is in takes it, and in Konsole its tab
(see terminal.py)."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

TILE_TOP, TILE_BOTTOM = (0xA9, 0x8B, 0xFF), (0x6A, 0x48, 0xD8)  # the Violet look's working and background
PANE = "#161122"
EDGE = "#C4AEFF"
LIT = "#F07BE0"  # needs you
SCALE = 8  # drawn this many times over, then shrunk: smooth corners, edges on whole pixels
TAB_SIZES = (16, 20, 24, 32, 40, 48)  # a tab's icon, on screens at 100% to 300%


def draw(size: int) -> Image.Image:
    s, big = SCALE, size * SCALE
    column = Image.new("RGB", (1, 256))
    for y in range(256):
        column.putpixel((0, y), tuple(round(a + (b - a) * y / 255) for a, b in zip(TILE_TOP, TILE_BOTTOM)))
    tile = Image.new("L", (big, big), 0)
    ImageDraw.Draw(tile).rounded_rectangle((0, 0, big - 1, big - 1), radius=round(big * 0.22), fill=255)
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    image.paste(column.resize((big, big)), mask=tile)

    pen = ImageDraw.Draw(image)
    margin, gap, pane = _layout(size)
    corner = round(pane * s * 0.12) if size >= 32 else 0
    edge = s * max(1, round(size / 40))
    for row in range(2):
        for col in range(2):
            x, y = (margin + col * (pane + gap)) * s, (margin + row * (pane + gap)) * s
            lit = (row, col) == (0, 0)
            pen.rounded_rectangle((x, y, x + pane * s - 1, y + pane * s - 1), radius=corner, fill=LIT if lit else PANE,
                                  outline=None if lit or size < 32 else EDGE, width=edge)
            if size >= 48:  # big enough for two lines of text in each
                for i, length in enumerate((0.62, 0.42)):
                    top = y + round(pane * s * (0.32 + i * 0.22))
                    pen.rectangle((x + round(pane * s * 0.2), top, x + round(pane * s * (0.2 + length)),
                                   top + max(s, round(pane * s * 0.08))), fill=PANE if lit else EDGE)
    return image.resize((size, size), Image.Resampling.BOX)


def png(size: int) -> bytes:
    out = io.BytesIO()
    draw(size).save(out, "PNG")
    return out.getvalue()


def ico(sizes: tuple[int, ...] = TAB_SIZES) -> bytes:
    """The icon as an .ico file, which holds it drawn for each of these sizes."""
    from PIL import IcoImagePlugin  # noqa: F401 - Pillow's writer for these, and not for every other format

    *smaller, largest = [draw(size) for size in sorted(sizes)]
    out = io.BytesIO()
    largest.save(out, "ICO", sizes=[image.size for image in (*smaller, largest)], append_images=smaller)
    return out.getvalue()


def _layout(size: int) -> tuple[int, int, int]:
    """The margin, the gap between panes and a pane's side, in whole pixels."""
    margin, gap = max(2, round(size * 0.14)), max(2, round(size * 0.09))
    if (size - 2 * margin - gap) % 2:
        gap += 1
    return margin, gap, (size - 2 * margin - gap) // 2
