"""Image-region diffing to verify a target has not changed before clicking."""

from __future__ import annotations

from PIL import Image, ImageChops


def crop_around(
    image: Image.Image,
    center: tuple[int, int],
    size: tuple[int, int] = (120, 120),
) -> Image.Image:
    """Return a crop around ``center``, clamped to image bounds."""

    cx, cy = center
    width, height = size
    left = max(0, cx - width // 2)
    top = max(0, cy - height // 2)
    right = min(image.width, left + width)
    bottom = min(image.height, top + height)
    return image.crop((left, top, right, bottom))


def pixel_similarity(
    a: Image.Image,
    b: Image.Image,
    *,
    threshold: int = 30,
) -> float:
    """Return pixel-level similarity in [0.0, 1.0].

    Two pixels are considered similar when their grayscale difference is
    below ``threshold``. This is robust to minor compression/anti-aliasing
    differences while still detecting real UI changes.
    """

    if a.size != b.size:
        b = b.resize(a.size, Image.Resampling.LANCZOS)
    if a.mode != "L":
        a = a.convert("L")
    if b.mode != "L":
        b = b.convert("L")

    diff = ImageChops.difference(a, b)
    histogram = diff.histogram()
    similar_pixels = sum(histogram[:threshold])
    total = a.width * a.height
    return similar_pixels / total if total else 1.0


def make_diff_image(
    reference: Image.Image,
    current: Image.Image,
    target: tuple[int, int],
    *,
    crop_size: tuple[int, int] = (120, 120),
    pixel_threshold: int = 30,
) -> Image.Image:
    """Return a side-by-side diff image for the region around ``target``.

    Changed pixels are overlaid in red so a human can see what moved before
    the click was attempted.
    """

    ref = crop_around(reference, target, crop_size).convert("RGB")
    cur = crop_around(current, target, crop_size).convert("RGB")
    if ref.size != cur.size:
        cur = cur.resize(ref.size, Image.Resampling.LANCZOS)

    diff = ImageChops.difference(ref, cur).convert("L")
    mask = diff.point(lambda p: 255 if p > pixel_threshold else 0)
    red = Image.new("RGB", ref.size, (255, 0, 0))
    highlighted = Image.composite(red, cur, mask)

    # side-by-side: reference | current-with-red-changes
    width = ref.width * 2
    height = ref.height
    canvas = Image.new("RGB", (width, height), (30, 30, 30))
    canvas.paste(ref, (0, 0))
    canvas.paste(highlighted, (ref.width, 0))
    return canvas


def verify_target_stable(
    reference: Image.Image,
    current: Image.Image,
    target: tuple[int, int],
    *,
    crop_size: tuple[int, int] = (120, 120),
    threshold: float = 0.8,
) -> tuple[bool, float]:
    """Compare a small region around ``target`` in two images.

    Returns ``(stable, similarity)`` where ``stable`` is True when the
    similarity is at least ``threshold``.
    """

    ref_crop = crop_around(reference, target, crop_size)
    cur_crop = crop_around(current, target, crop_size)
    similarity = pixel_similarity(ref_crop, cur_crop)
    return similarity >= threshold, similarity
