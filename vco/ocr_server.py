"""Small standard-library HTTP server for the portable VCO OCR contract."""

from __future__ import annotations

import base64
import binascii
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from .ocr import OCRRequest


MAX_IMAGE_BYTES = 20 * 1024 * 1024


def recognize_http_payload(backend, payload: object) -> dict:
    """Validate one wire request and return the normalized OCR response."""

    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    encoded = payload.get("image_base64")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("image_base64 is required")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image_base64 is invalid") from exc
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(f"decoded image exceeds {MAX_IMAGE_BYTES} bytes")
    try:
        with Image.open(io.BytesIO(raw)) as source:
            image = source.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("image_base64 is not a supported image") from exc
    request = OCRRequest(
        languages=tuple(payload.get("languages") or ("zh-Hans", "en-US")),
        mode=str(payload.get("mode", "accurate")),
        custom_words=tuple(payload.get("custom_words") or ()),
        min_confidence=float(payload.get("min_confidence", 0.0)),
    )
    return backend.recognize(image, request).model_dump(mode="json")


def make_handler(backend, *, api_key: str | None = None):
    class OCRHandler(BaseHTTPRequestHandler):
        server_version = "VCO-OCR/1"

        def _json(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._json(200, {"status": "ok", "backend": backend.name})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/v1/ocr":
                self._json(404, {"error": "not found"})
                return
            if api_key and self.headers.get("Authorization") != f"Bearer {api_key}":
                self._json(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_IMAGE_BYTES * 2:
                    raise ValueError("invalid Content-Length")
                payload = json.loads(self.rfile.read(length))
                result = recognize_http_payload(backend, payload)
            except (ValueError, TypeError, json.JSONDecodeError, ValidationError) as exc:
                self._json(400, {"error": str(exc)})
                return
            except Exception as exc:
                self._json(500, {"error": f"OCR failed: {type(exc).__name__}"})
                return
            self._json(200, result)

        def log_message(self, format, *args):
            return

    return OCRHandler


def serve_ocr(
    backend,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    api_key: str | None = None,
) -> None:
    """Serve /health and /v1/ocr until interrupted."""

    if host not in {"127.0.0.1", "localhost", "::1"} and not api_key:
        raise ValueError("a bearer token is required when binding outside localhost")
    server = ThreadingHTTPServer((host, port), make_handler(backend, api_key=api_key))
    print(f"VCO OCR listening on http://{host}:{port}/v1/ocr ({backend.name})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
