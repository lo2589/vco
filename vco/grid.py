"""Render a readable, semi-transparent numbered grid over a screenshot."""

from __future__ import annotations

import math
from typing import Tuple

from PIL import Image, ImageDraw, ImageFont

from .models import GridSpec


def _font(size: int):
    candidates = (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "DejaVuSans-Bold.ttf",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def _cell_edges(length: int, count: int):
    return [math.floor(i * length / count) for i in range(count + 1)]


def render_numbered_grid(
    image: Image.Image,
    grid: GridSpec,
    *,
    line_color: Tuple[int, int, int, int] = (112, 128, 144, 82),
    shade_color: Tuple[int, int, int, int] = (70, 85, 100, 10),
    label_position: str = "top_left",
) -> Image.Image:
    """Return a new RGBA image; the clean source is never mutated."""

    if image.width < grid.cols or image.height < grid.rows:
        raise ValueError("image must contain at least one pixel per grid cell")
    if label_position not in {"top_left", "center"}:
        raise ValueError("label_position must be 'top_left' or 'center'")

    base = image.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    x_edges = _cell_edges(base.width, grid.cols)
    y_edges = _cell_edges(base.height, grid.rows)
    shortest_cell = min(base.width / grid.cols, base.height / grid.rows)
    font_size = max(10, min(24, round(shortest_cell * 0.24)))
    font = _font(font_size)
    # Thin, low-chroma lines preserve small UI details. Labels carry the
    # coordinate information, so the line itself should behave like a ruler
    # rather than a foreground object.
    stroke_width = 1

    for row in range(grid.rows):
        for col in range(grid.cols):
            left, right = x_edges[col], x_edges[col + 1]
            top, bottom = y_edges[row], y_edges[row + 1]
            if (row + col) % 2 == 0:
                draw.rectangle((left, top, right - 1, bottom - 1), fill=shade_color)

            label = str(row * grid.cols + col + 1)
            bbox = draw.textbbox((0, 0), label, font=font, stroke_width=1)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            pad_x, pad_y = 3, 2
            badge_width = max(
                1, min(max(1, right - left - 2), text_w + pad_x * 2)
            )
            badge_height = max(
                1, min(max(1, bottom - top - 2), text_h + pad_y * 2)
            )
            if label_position == "center":
                badge_left = left + (right - left - badge_width) // 2
                badge_top = top + (bottom - top - badge_height) // 2
            else:
                badge_left = left + 1
                badge_top = top + 1
            badge_right = badge_left + badge_width
            badge_bottom = badge_top + badge_height
            draw.rounded_rectangle(
                (badge_left, badge_top, badge_right, badge_bottom),
                radius=3,
                fill=(0, 0, 0, 175),
            )
            draw.text(
                (badge_left + pad_x, badge_top + pad_y - bbox[1]),
                label,
                font=font,
                fill=(255, 255, 255, 255),
                stroke_width=1,
                stroke_fill=(0, 0, 0, 255),
            )

    for x in x_edges:
        draw.line((x, 0, x, base.height - 1), fill=line_color, width=stroke_width)
    for y in y_edges:
        draw.line((0, y, base.width - 1, y), fill=line_color, width=stroke_width)
    draw.rectangle(
        (0, 0, base.width - 1, base.height - 1),
        outline=line_color,
        width=stroke_width,
    )
    return Image.alpha_composite(base, overlay)
