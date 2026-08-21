"""OCR-first wrapper for grid-based vision providers."""

from __future__ import annotations

import json

from PIL import Image

from .geometry import GridMapper
from .models import Action, GridSpec, Region, parse_action
from .ocr import OCRBackend, OCRRequest, OCRResult


class OCRAssistProvider:
    """Use exact local OCR when possible, otherwise add OCR hints to the VLM task."""

    def __init__(
        self,
        provider,
        backend: OCRBackend,
        *,
        request: OCRRequest | None = None,
        target_text: str | None = None,
        exact_match: bool = True,
        direct_click: bool = True,
        max_hints: int = 100,
        hint_texts: tuple[str, ...] = (),
    ):
        if max_hints < 1:
            raise ValueError("max_hints must be at least 1")
        self.provider = provider
        self.backend = backend
        self.request = request or OCRRequest()
        self.target_text = target_text.strip() if target_text else None
        self.exact_match = exact_match
        self.direct_click = direct_click
        self.max_hints = max_hints
        self.hint_texts = tuple(text.strip().casefold() for text in hint_texts if text.strip())
        self.last_result: OCRResult | None = None
        self.last_metadata: dict | None = None

    @staticmethod
    def _point_for_box(box, image: Image.Image, grid: GridSpec):
        x, y = box.center
        mapper = GridMapper(Region(width=image.width, height=image.height), grid)
        return mapper.from_screen(
            min(image.width - 1, max(0, round(x))),
            min(image.height - 1, max(0, round(y))),
        )

    def _ocr(self, clean: Image.Image) -> tuple[OCRResult | None, str | None]:
        try:
            result = self.backend.recognize(clean, self.request)
        except Exception as exc:
            self.last_result = None
            return None, f"{type(exc).__name__}: {exc}"
        self.last_result = result
        return result, None

    def _enriched_task(
        self, task: str, result: OCRResult, clean: Image.Image, grid: GridSpec
    ) -> str:
        hints = []
        boxes = result.boxes
        if self.hint_texts:
            boxes = tuple(
                box
                for box in boxes
                if any(hint in box.text.casefold() for hint in self.hint_texts)
            )
        for box in boxes[: self.max_hints]:
            point = self._point_for_box(box, clean, grid)
            hints.append(
                {
                    "id": box.id,
                    "text": box.text,
                    "confidence": round(box.confidence, 3),
                    "grid_target": point.model_dump(mode="json"),
                }
            )
        return (
            task
            + "\nLocal OCR hints follow. They may contain recognition errors; verify "
            "them against the images. Coordinates already use the required grid "
            "protocol, not screen pixels:\n"
            + json.dumps(hints, ensure_ascii=False, separators=(",", ":"))
        )

    def _choose(
        self,
        method_name: str,
        *,
        task: str,
        clean: Image.Image,
        gridded: Image.Image,
        grid: GridSpec,
        step: int,
        force_click: bool = False,
    ):
        result, error = self._ocr(clean)
        matches = ()
        if result is not None and self.target_text:
            matches = result.find_text(
                self.target_text,
                exact=self.exact_match,
                min_confidence=self.request.min_confidence,
            )
        if self.direct_click and len(matches) == 1:
            point = self._point_for_box(matches[0], clean, grid)
            action = parse_action(
                {"type": "click", "target": point.model_dump(mode="json")}
            )
            self.last_metadata = {
                "ocr_backend": self.backend.name,
                "ocr_elapsed_ms": result.elapsed_ms,
                "ocr_box_count": len(result.boxes),
                "ocr_target": self.target_text,
                "ocr_matches": [matches[0].id],
                "ocr_direct": True,
                "model_calls": 0,
                "elapsed_seconds": round(result.elapsed_ms / 1000, 3),
            }
            return action

        enriched = task if result is None else self._enriched_task(task, result, clean, grid)
        method = getattr(self.provider, method_name)
        decision = method(
            task=enriched,
            clean=clean,
            gridded=gridded,
            grid=grid,
            step=step,
            force_click=force_click,
        )
        upstream_metadata = getattr(self.provider, "last_metadata", None)
        ocr_elapsed_seconds = 0 if result is None else result.elapsed_ms / 1000
        upstream_elapsed_seconds = (upstream_metadata or {}).get(
            "elapsed_seconds", 0
        )
        self.last_metadata = {
            "ocr_backend": self.backend.name,
            "ocr_elapsed_ms": None if result is None else result.elapsed_ms,
            "ocr_box_count": 0 if result is None else len(result.boxes),
            "ocr_target": self.target_text,
            "ocr_matches": [box.id for box in matches],
            "ocr_direct": False,
            "ocr_error": error,
            "upstream": upstream_metadata,
            "elapsed_seconds": round(
                ocr_elapsed_seconds + upstream_elapsed_seconds, 3
            ),
        }
        return decision

    def choose_action(
        self,
        *,
        task: str,
        clean: Image.Image,
        gridded: Image.Image,
        grid: GridSpec,
        step: int,
        force_click: bool = False,
    ) -> Action:
        return self._choose(
            "choose_action",
            task=task,
            clean=clean,
            gridded=gridded,
            grid=grid,
            step=step,
            force_click=force_click,
        )

    def choose_action_or_zoom(
        self,
        *,
        task: str,
        clean: Image.Image,
        gridded: Image.Image,
        grid: GridSpec,
        step: int,
        force_click: bool = False,
    ):
        return self._choose(
            "choose_action_or_zoom",
            task=task,
            clean=clean,
            gridded=gridded,
            grid=grid,
            step=step,
            force_click=force_click,
        )
