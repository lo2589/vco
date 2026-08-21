"""Numbered-grid visual computer operation prototype."""

from .geometry import GridMapper
from .models import Action, GridPoint, GridSpec, Region, parse_action
from .ocr import (
    HTTPOCRBackend,
    OCRBackend,
    OCRBox,
    OCRRequest,
    OCRResult,
    RapidOCRBackend,
    create_ocr_backend,
    register_ocr_backend,
)
from .ocr_assist import OCRAssistProvider

__all__ = [
    "Action",
    "GridMapper",
    "GridPoint",
    "GridSpec",
    "OCRBackend",
    "OCRAssistProvider",
    "OCRBox",
    "OCRRequest",
    "OCRResult",
    "HTTPOCRBackend",
    "RapidOCRBackend",
    "Region",
    "create_ocr_backend",
    "parse_action",
    "register_ocr_backend",
]

__version__ = "0.1.0"
