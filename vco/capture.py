"""Screen capture backend."""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageGrab

from .models import Region


@dataclass
class CaptureFrame:
    image: Image.Image
    region: Region


class PillowScreenCapture:
    """Capture a target region via Pillow's native macOS/Windows backend."""

    def capture(self, region: Region) -> CaptureFrame:
        bbox = (region.x, region.y, region.x + region.width, region.y + region.height)
        try:
            image = ImageGrab.grab(bbox=bbox).convert("RGB")
        except Exception as exc:
            raise RuntimeError(
                "screen capture failed; on macOS grant Screen Recording permission "
                "to the terminal or Codex app"
            ) from exc
        return CaptureFrame(image=image, region=region)

