"""Pluggable local OCR interface and built-in macOS Vision backend."""

from __future__ import annotations

import json
import base64
import contextlib
import importlib.util
import io
import platform
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from PIL import Image
from pydantic import Field, model_validator

from .models import StrictModel


class OCRRequest(StrictModel):
    """Backend-neutral OCR options."""

    languages: tuple[str, ...] = ("zh-Hans", "en-US")
    mode: str = "accurate"
    custom_words: tuple[str, ...] = ()
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class OCRBox(StrictModel):
    """One OCR observation in image-local pixel coordinates."""

    id: str
    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    bbox: tuple[float, float, float, float]

    @model_validator(mode="after")
    def _ordered_box(self):
        left, top, right, bottom = self.bbox
        if right < left or bottom < top:
            raise ValueError("bbox must be ordered as left,top,right,bottom")
        return self

    @property
    def center(self) -> tuple[float, float]:
        left, top, right, bottom = self.bbox
        return ((left + right) / 2, (top + bottom) / 2)


class OCRResult(StrictModel):
    """Normalized output shared by all OCR backends."""

    backend: str
    mode: str
    image_size: tuple[int, int]
    elapsed_ms: float = Field(ge=0.0)
    boxes: tuple[OCRBox, ...]

    @model_validator(mode="after")
    def _boxes_inside_image(self):
        width, height = self.image_size
        if width <= 0 or height <= 0:
            raise ValueError("image_size must be positive")
        for box in self.boxes:
            left, top, right, bottom = box.bbox
            if left < 0 or top < 0 or right > width or bottom > height:
                raise ValueError(f"{box.id} bbox is outside the image")
        return self

    def find_text(
        self,
        query: str,
        *,
        exact: bool = True,
        case_sensitive: bool = False,
        min_confidence: float = 0.0,
    ) -> tuple[OCRBox, ...]:
        """Find OCR boxes locally without another model call."""

        needle = query.strip()
        if not case_sensitive:
            needle = needle.casefold()
        matches = []
        for box in self.boxes:
            if box.confidence < min_confidence:
                continue
            candidate = box.text.strip()
            if not case_sensitive:
                candidate = candidate.casefold()
            matched = candidate == needle if exact else needle in candidate
            if matched:
                matches.append(box)
        return tuple(matches)


@runtime_checkable
class OCRBackend(Protocol):
    """Stable interface implemented by Apple Vision, ONNX, or remote OCR."""

    name: str

    def recognize(
        self, image: Image.Image, request: OCRRequest | None = None
    ) -> OCRResult:
        """Recognize text and return image-local boxes."""


OCRBackendFactory = Callable[..., OCRBackend]
_OCR_BACKENDS: dict[str, OCRBackendFactory] = {}


def register_ocr_backend(
    name: str,
    factory: OCRBackendFactory,
    *,
    replace: bool = False,
) -> None:
    """Register a named OCR adapter, for example a RapidOCR ONNX backend."""

    normalized = name.strip().lower()
    if not normalized:
        raise ValueError("OCR backend name cannot be empty")
    if normalized in _OCR_BACKENDS and not replace:
        raise ValueError(f"OCR backend is already registered: {normalized}")
    _OCR_BACKENDS[normalized] = factory


def parse_ocr_payload(
    payload: object,
    *,
    backend: str,
    min_confidence: float = 0.0,
) -> OCRResult:
    """Normalize the simple JSON contract emitted by OCR adapters."""

    if not isinstance(payload, dict):
        raise ValueError("OCR output must be a JSON object")
    width, height = payload["image_size"]
    boxes = []
    for index, item in enumerate(payload.get("results", ()), start=1):
        confidence = float(item["confidence"])
        if confidence < min_confidence:
            continue
        boxes.append(
            OCRBox(
                id=f"T{index}",
                text=str(item["text"]),
                confidence=confidence,
                bbox=tuple(float(value) for value in item["bbox"]),
            )
        )
    return OCRResult(
        backend=backend,
        mode=str(payload.get("mode", "unknown")),
        image_size=(int(width), int(height)),
        elapsed_ms=float(payload.get("elapsed_ms", 0.0)),
        boxes=tuple(boxes),
    )


class AppleVisionOCR:
    """macOS OCR backend implemented with Vision's VNRecognizeTextRequest."""

    name = "apple-vision"

    def __init__(self, *, timeout: float = 30.0, swift: str = "swift"):
        self.timeout = timeout
        self.swift = swift

    @property
    def available(self) -> bool:
        return platform.system() == "Darwin" and shutil.which(self.swift) is not None

    def recognize(
        self, image: Image.Image, request: OCRRequest | None = None
    ) -> OCRResult:
        options = request or OCRRequest()
        if not self.available:
            raise RuntimeError("Apple Vision OCR requires macOS and the swift command")
        if options.mode not in {"fast", "accurate"}:
            raise ValueError("Apple Vision mode must be fast or accurate")

        script = Path(__file__).with_name("backends") / "apple_vision_ocr.swift"
        with tempfile.TemporaryDirectory(prefix="vco-ocr-") as temp_dir:
            image_path = Path(temp_dir) / "input.png"
            image.convert("RGB").save(image_path)
            command = [
                self.swift,
                "-module-cache-path",
                str(Path(temp_dir) / "swift-cache"),
                str(script),
                str(image_path),
                options.mode,
                ",".join(options.custom_words),
                ",".join(options.languages),
            ]
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "unknown Swift error"
            raise RuntimeError(f"Apple Vision OCR failed: {detail}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Apple Vision OCR returned invalid JSON") from exc
        return parse_ocr_payload(
            payload,
            backend=self.name,
            min_confidence=options.min_confidence,
        )


class RapidOCRBackend:
    """Cross-platform OCR using RapidOCR with an ONNX Runtime engine."""

    name = "rapidocr"

    def __init__(self, *, timeout: float = 30.0, engine=None):
        self.timeout = timeout
        self._engine = engine
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        if self._engine is not None:
            return True
        return (
            importlib.util.find_spec("rapidocr") is not None
            and importlib.util.find_spec("onnxruntime") is not None
        )

    def _load_engine(self):
        if self._engine is None:
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    from rapidocr import RapidOCR
            except ImportError as exc:
                raise RuntimeError(
                    "RapidOCR requires: pip install rapidocr onnxruntime"
                ) from exc
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                self._engine = RapidOCR()
        return self._engine

    def recognize(
        self, image: Image.Image, request: OCRRequest | None = None
    ) -> OCRResult:
        options = request or OCRRequest()
        if not self.available:
            raise RuntimeError(
                "RapidOCR requires: pip install rapidocr onnxruntime"
            )
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="vco-rapidocr-") as temp_dir:
            image_path = Path(temp_dir) / "input.png"
            image.convert("RGB").save(image_path)
            with self._lock, contextlib.redirect_stdout(
                io.StringIO()
            ), contextlib.redirect_stderr(io.StringIO()):
                output = self._load_engine()(
                    str(image_path), use_cls=options.mode != "fast"
                )
        elapsed_ms = (time.perf_counter() - started) * 1000

        raw_boxes = getattr(output, "boxes", None)
        texts = getattr(output, "txts", None) or ()
        scores = getattr(output, "scores", None) or ()
        boxes = []
        if raw_boxes is not None:
            for index, (quad, text_value, score_value) in enumerate(
                zip(raw_boxes, texts, scores), start=1
            ):
                confidence = float(score_value)
                if confidence < options.min_confidence:
                    continue
                points = [(float(point[0]), float(point[1])) for point in quad]
                left = max(0.0, min(point[0] for point in points))
                top = max(0.0, min(point[1] for point in points))
                right = min(float(image.width), max(point[0] for point in points))
                bottom = min(float(image.height), max(point[1] for point in points))
                boxes.append(
                    OCRBox(
                        id=f"T{index}",
                        text=str(text_value),
                        confidence=confidence,
                        bbox=(left, top, right, bottom),
                    )
                )
        return OCRResult(
            backend=self.name,
            mode=options.mode,
            image_size=image.size,
            elapsed_ms=elapsed_ms,
            boxes=tuple(boxes),
        )


class HTTPOCRBackend:
    """Portable JSON-over-HTTP OCR client using the VCO OCR wire contract."""

    name = "http"

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str | None = None,
        timeout: float = 30.0,
        opener=None,
    ):
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("OCR endpoint must start with http:// or https://")
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    @property
    def available(self) -> bool:
        return True

    def recognize(
        self, image: Image.Image, request: OCRRequest | None = None
    ) -> OCRResult:
        options = request or OCRRequest()
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        body = json.dumps(
            {
                "image_base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
                "image_format": "png",
                "languages": list(options.languages),
                "mode": options.mode,
                "custom_words": list(options.custom_words),
                "min_confidence": options.min_confidence,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        http_request = urllib.request.Request(
            self.endpoint, data=body, headers=headers, method="POST"
        )
        try:
            with self._opener(http_request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"HTTP OCR request failed: {exc}") from exc
        if isinstance(payload, dict) and "boxes" in payload:
            result = OCRResult.model_validate(payload)
        else:
            result = parse_ocr_payload(
                payload,
                backend=self.name,
                min_confidence=options.min_confidence,
            )
        if result.image_size != image.size:
            raise RuntimeError(
                f"HTTP OCR image_size {result.image_size} does not match {image.size}"
            )
        return result


def create_ocr_backend(
    name: str = "auto", *, timeout: float = 30.0, **options
) -> OCRBackend:
    """Create a backend without exposing platform choices to callers."""

    normalized = name.strip().lower()
    if normalized == "auto":
        rapid = RapidOCRBackend(timeout=timeout)
        if rapid.available:
            return rapid
        backend = AppleVisionOCR(timeout=timeout)
        if backend.available:
            return backend
        raise RuntimeError(
            "no automatic OCR backend is available on this platform; select a "
            "registered backend explicitly"
        )
    factory = _OCR_BACKENDS.get(normalized)
    if factory is not None:
        backend = factory(timeout=timeout, **options)
        if not getattr(backend, "available", True):
            if normalized == "apple-vision":
                raise RuntimeError("Apple Vision OCR is unavailable on this machine")
            if normalized == "rapidocr":
                raise RuntimeError(
                    "RapidOCR requires: pip install rapidocr onnxruntime"
                )
            raise RuntimeError(f"OCR backend is unavailable: {normalized}")
        return backend
    raise RuntimeError(
        f"unknown OCR backend {normalized!r}; register an ONNX adapter first"
    )


register_ocr_backend("apple-vision", AppleVisionOCR)
register_ocr_backend("rapidocr", RapidOCRBackend)
register_ocr_backend("http", HTTPOCRBackend)
