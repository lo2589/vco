"""Generate the deterministic UI image used by the local vision smoke test."""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("probe-clean.png"))
    args = parser.parse_args()

    image = Image.new("RGB", (1000, 600), (238, 242, 247))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (70, 55, 930, 545),
        radius=18,
        fill="white",
        outline=(190, 200, 212),
        width=2,
    )
    draw.text((110, 95), "vco - Demo", fill=(25, 35, 48))
    for index, label in enumerate(("Open project", "Run task", "Review result")):
        y = 165 + index * 88
        draw.rounded_rectangle(
            (110, y, 890, y + 58),
            radius=9,
            fill=(245, 247, 250),
            outline=(210, 218, 228),
        )
        draw.text((140, y + 20), label, fill=(45, 55, 70))
    draw.rounded_rectangle((720, 475, 890, 520), radius=9, fill=(35, 120, 235))
    draw.text((777, 491), "Save", fill="white")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output)
    print(args.output)


if __name__ == "__main__":
    main()

