"""Generate ten deterministic UI screenshots with click ground truth."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WIDTH = 1000
HEIGHT = 600


def font(size: int, bold: bool = False):
    candidates = (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        if bold
        else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


CASES = (
    ("01", "Save", "blue", (720, 475, 890, 520), (35, 120, 235)),
    ("02", "Continue", "green", (755, 88, 920, 138), (30, 155, 105)),
    ("03", "Delete", "red", (82, 272, 237, 322), (210, 65, 70)),
    ("04", "Settings", "purple", (78, 88, 248, 138), (120, 80, 190)),
    ("05", "Submit", "orange", (420, 270, 575, 320), (225, 125, 35)),
    ("06", "Next", "cyan", (80, 472, 225, 520), (20, 155, 185)),
    ("07", "Download", "blue", (750, 270, 925, 320), (45, 105, 210)),
    ("08", "Cancel", "gray", (425, 88, 575, 138), (95, 105, 118)),
    ("09", "Apply", "green", (420, 472, 575, 520), (35, 150, 90)),
    ("10", "Upload", "purple", (80, 365, 235, 415), (130, 75, 185)),
)

DECOYS = (
    ("Back", (80, 185, 225, 233), (110, 120, 132)),
    ("Preview", (420, 185, 575, 233), (45, 105, 210)),
    ("Help", (755, 185, 920, 233), (20, 145, 170)),
    ("Reset", (80, 472, 225, 520), (205, 95, 45)),
    ("Close", (755, 472, 920, 520), (105, 112, 122)),
)


def draw_button(draw, box, label, color, *, target=False):
    draw.rounded_rectangle(box, radius=10, fill=color)
    label_font = font(19, bold=target)
    bounds = draw.textbbox((0, 0), label, font=label_font)
    text_width = bounds[2] - bounds[0]
    text_height = bounds[3] - bounds[1]
    x = box[0] + (box[2] - box[0] - text_width) / 2
    y = box[1] + (box[3] - box[1] - text_height) / 2 - bounds[1]
    draw.text((x, y), label, font=label_font, fill="white")


def render_case(case):
    case_id, label, color_name, target_box, target_color = case
    image = Image.new("RGB", (WIDTH, HEIGHT), (236, 241, 247))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (45, 42, 955, 552),
        radius=20,
        fill="white",
        outline=(185, 197, 212),
        width=2,
    )
    draw.text((78, 58), f"Workspace {case_id}", font=font(23, bold=True), fill=(25, 35, 48))
    draw.text(
        (78, 145),
        "Choose the requested action",
        font=font(17),
        fill=(92, 103, 116),
    )
    for decoy_label, decoy_box, decoy_color in DECOYS:
        if _overlap(decoy_box, target_box):
            continue
        draw_button(draw, decoy_box, decoy_label, decoy_color)
    draw_button(draw, target_box, label, target_color, target=True)
    task = f"Click the {color_name} {label} button."
    return image, {
        "id": case_id,
        "filename": f"case-{case_id}.png",
        "task": task,
        "target_label": label,
        "target_color": color_name,
        "target_bbox": list(target_box),
    }


def _overlap(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("debug/cases"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    manifest = []
    for case in CASES:
        image, metadata = render_case(case)
        image.save(args.output / metadata["filename"])
        manifest.append(metadata)
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"generated {len(manifest)} cases in {args.output}")


if __name__ == "__main__":
    main()

