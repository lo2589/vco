#!/usr/bin/env python3
"""Run one cached OCR-first track per vision provider."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
DEBUG = ROOT / "debug"
for path in (ROOT, DEBUG):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from run_web_ui_matrix import (
    DEFAULT_SETTINGS_DIR,
    Configuration,
    build_providers,
    clean_error,
    mark_with_cv,
    resolve_prediction,
    write_json,
)
from vco.geometry import GridMapper
from vco.grid import render_numbered_grid
from vco.models import GridSpec, Region
from vco.ocr import OCRRequest, OCRResult, RapidOCRBackend
from vco.zoom import ZoomProvider


def normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def matching_boxes(result: OCRResult, query: str | None):
    if not query:
        return ()
    needle = normalize_text(query)
    return tuple(
        box
        for box in result.boxes
        if needle and needle in normalize_text(box.text)
    )


def load_or_run_ocr(
    backend: RapidOCRBackend, clean: Image.Image, cache_path: Path
) -> OCRResult:
    if cache_path.exists():
        return OCRResult.model_validate_json(cache_path.read_text(encoding="utf-8"))
    result = backend.recognize(
        clean, OCRRequest(mode="accurate", min_confidence=0.35)
    )
    write_json(cache_path, result.model_dump(mode="json"))
    return result


def candidate_grid_hints(matches, clean: Image.Image, grid_n: int) -> list[dict]:
    mapper = GridMapper(
        Region(width=clean.width, height=clean.height),
        GridSpec(rows=grid_n, cols=grid_n),
    )
    hints = []
    for index, box in enumerate(matches):
        x, y = box.center
        point = mapper.from_screen(round(x), round(y))
        hints.append(
            {
                "ocr_id": box.id,
                "text": box.text,
                "confidence": round(box.confidence, 3),
                "visual_marker": chr(ord("A") + index),
                "grid_target": point.model_dump(mode="json"),
            }
        )
    return hints


def render_candidate_overlay(gridded: Image.Image, matches) -> Image.Image:
    """Add small translucent candidate regions while preserving text and grid."""

    palette = (
        (66, 165, 245),
        (255, 183, 77),
        (171, 71, 188),
        (102, 187, 106),
    )
    base = gridded.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    try:
        font = ImageFont.truetype(
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf", 14
        )
    except OSError:
        font = ImageFont.load_default()
    for index, box in enumerate(matches):
        red, green, blue = palette[index % len(palette)]
        left, top, right, bottom = box.bbox
        draw.rounded_rectangle(
            (left, top, right, bottom),
            radius=3,
            fill=(red, green, blue, 58),
            outline=(red, green, blue, 180),
            width=2,
        )
        marker = chr(ord("A") + index)
        badge_left = max(0, round(left) - 2)
        badge_top = max(0, round(top) - 18)
        draw.rounded_rectangle(
            (badge_left, badge_top, badge_left + 18, badge_top + 17),
            radius=3,
            fill=(red, green, blue, 190),
        )
        draw.text(
            (badge_left + 4, badge_top + 1),
            marker,
            font=font,
            fill=(255, 255, 255, 255),
        )
    return Image.alpha_composite(base, overlay).convert("RGB")


def call_small_zoom(
    provider,
    task: str,
    clean: Image.Image,
    run_dir: Path,
    *,
    coarse_n: int,
    fine_n: int,
    span_cells: float,
):
    coarse_grid = GridSpec(rows=coarse_n, cols=coarse_n)
    fine_grid = GridSpec(rows=fine_n, cols=fine_n)
    zoom = ZoomProvider(
        provider,
        fine_grid=fine_grid,
        span_cells=span_cells,
        use_model_offset=False,
        force_initial_click=True,
        resize_crop_to_input=True,
    )
    action = zoom.choose_action(
        task=task,
        clean=clean,
        gridded=render_numbered_grid(clean, coarse_grid),
        grid=coarse_grid,
        step=1,
    )
    if zoom.last_zoom_clean is not None:
        zoom.last_zoom_clean.save(run_dir / "fallback-zoom-clean.png")
    if zoom.last_zoom_grid is not None:
        zoom.last_zoom_grid.save(run_dir / "fallback-zoom-grid.png")
    point = resolve_prediction(action, clean, coarse_grid)
    return (
        action,
        point,
        zoom.last_trace.as_dict() if zoom.last_trace else None,
        getattr(provider, "last_metadata", None),
    )


def reusable_candidate_result(
    case_dir: Path, provider_name: str, version: str | None
):
    if not version:
        return None
    pattern = f"{provider_name}-ocr-first-{version}__*/result.json"
    for result_path in sorted(case_dir.glob(pattern)):
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        metadata = result.get("provider_metadata") or {}
        if (
            result.get("status") == "ok"
            and result.get("predicted_point_px")
            and metadata.get("candidate_selected") is not None
        ):
            return result_path, result
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("cache/web-ui-matrix"))
    parser.add_argument("--settings-dir", type=Path, default=DEFAULT_SETTINGS_DIR)
    parser.add_argument(
        "--providers", default="glm,minimax,minicpm,gemma4", help="comma-separated"
    )
    parser.add_argument(
        "--queries",
        type=Path,
        default=Path("example/web_ui_ocr_queries.json"),
    )
    parser.add_argument("--version", default="v8-fine25-span3-to6")
    parser.add_argument("--fallback-grid", type=int, default=25)
    parser.add_argument("--fallback-fine-grid", type=int, default=6)
    parser.add_argument("--fallback-span", type=float, default=3.0)
    parser.add_argument("--progress-file", default="progress-ocr-smallzoom.json")
    parser.add_argument(
        "--reuse-candidate-version",
        help="reuse already checked non-null OCR candidate results from this version",
    )
    parser.add_argument(
        "--direct-ocr",
        action="store_true",
        help="allow one exact OCR match to bypass model confirmation",
    )
    args = parser.parse_args()

    selected = {item.strip() for item in args.providers.split(",") if item.strip()}
    providers = build_providers(args.settings_dir, selected)
    manifest = json.loads((args.root / "manifest.json").read_text(encoding="utf-8"))
    queries = json.loads(args.queries.read_text(encoding="utf-8"))
    cache_dir = args.root / "ocr-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    backend = RapidOCRBackend(timeout=90)
    total = len(manifest) * len(providers)
    completed = 0

    for case in manifest:
        case_dir = args.root / case["folder"]
        clean_path = case_dir / case.get("filename", "clean.png")
        with Image.open(clean_path) as source:
            clean = source.convert("RGB")
        ocr = load_or_run_ocr(
            backend, clean, cache_dir / f"{case['folder']}.json"
        )
        query = queries.get(case["id"])
        matches = matching_boxes(ocr, query)

        for provider_name, provider in providers.items():
            config = Configuration(
                f"ocr-check-smallzoom-to{args.fallback_fine_grid:02d}",
                args.fallback_grid,
                2,
            )
            track_name = f"{provider_name}-ocr-first-{args.version}"
            run_name = f"{track_name}__{config.name}"
            run_dir = case_dir / run_name
            result_path = run_dir / "result.json"
            if result_path.exists():
                completed += 1
                continue
            run_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "case_id": case["id"],
                "task": case["task"],
                "provider": track_name,
                "model": provider.model,
                "mode": config.mode,
                "grid": {"rows": config.grid_n, "cols": config.grid_n},
                "levels": config.levels,
                "source_image": "../clean.png",
                "ocr": {
                    "backend": ocr.backend,
                    "query": query,
                    "box_count": len(ocr.boxes),
                    "match_count": len(matches),
                    "elapsed_ms": ocr.elapsed_ms,
                },
                "fallback": {
                    "grid_n": args.fallback_grid,
                    "fine_grid_n": args.fallback_fine_grid,
                    "levels": 2,
                    "span_cells": args.fallback_span,
                    "final_point": "cell-center",
                },
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            started = time.perf_counter()
            try:
                reusable = reusable_candidate_result(
                    case_dir, provider_name, args.reuse_candidate_version
                )
                if reusable is not None:
                    reused_path, reused = reusable
                    point = tuple(reused["predicted_point_px"])
                    action = None
                    trace = None
                    metadata = dict(reused.get("provider_metadata") or {})
                    metadata["reused_candidate_result_from"] = str(reused_path)
                    if matches:
                        render_candidate_overlay(clean, matches).save(
                            run_dir / "ocr-candidates.png"
                        )
                elif args.direct_ocr and len(matches) == 1:
                    x, y = matches[0].center
                    point = (
                        min(clean.width - 1, max(0, round(x))),
                        min(clean.height - 1, max(0, round(y))),
                    )
                    action = None
                    trace = None
                    metadata = {
                        "ocr_direct": True,
                        "ocr_match_id": matches[0].id,
                        "model_calls": 0,
                    }
                else:
                    hints = candidate_grid_hints(matches, clean, config.grid_n)
                    selected = None
                    selector_error = None
                    selector_metadata = None
                    if matches:
                        candidate_image = render_candidate_overlay(clean, matches)
                        candidate_image.save(run_dir / "ocr-candidates.png")
                        candidates = [
                            {
                                "id": chr(ord("A") + index),
                                "text": box.text,
                                "confidence": box.confidence,
                            }
                            for index, box in enumerate(matches)
                        ]
                        try:
                            selected = provider.choose_candidate(
                                task=case["task"],
                                clean=clean,
                                marked=candidate_image,
                                candidates=candidates,
                                step=1,
                            )
                            selector_metadata = getattr(
                                provider, "last_metadata", None
                            )
                        except Exception as exc:
                            selector_error = clean_error(exc)
                    if selected is not None:
                        selected_index = ord(selected) - ord("A")
                        if not 0 <= selected_index < len(matches):
                            raise ValueError("selected OCR candidate is out of range")
                        x, y = matches[selected_index].center
                        point = (
                            min(clean.width - 1, max(0, round(x))),
                            min(clean.height - 1, max(0, round(y))),
                        )
                        action = None
                        trace = None
                        metadata = {
                            "ocr_direct": False,
                            "candidate_selected": selected,
                            "candidate_overlay": True,
                            "candidate_markers": hints,
                            "model_calls": 1,
                            "selector": selector_metadata,
                        }
                    else:
                        reason = (
                            "The OCR candidate verifier returned null or failed; "
                            "locate the target normally from the screenshot."
                            if matches
                            else "The target has no OCR candidate; locate it normally."
                        )
                        action, point, trace, fallback_metadata = call_small_zoom(
                            provider,
                            case["task"] + "\nOCR fallback: " + reason,
                            clean,
                            run_dir,
                            coarse_n=args.fallback_grid,
                            fine_n=args.fallback_fine_grid,
                            span_cells=args.fallback_span,
                        )
                        metadata = {
                            "ocr_direct": False,
                            "candidate_selected": None,
                            "candidate_overlay": bool(matches),
                            "candidate_markers": hints,
                            "candidate_selector_error": selector_error,
                            "model_calls": (1 if matches else 0) + config.levels,
                            "selector": selector_metadata,
                            "fallback": fallback_metadata,
                        }
                mark_with_cv(clean_path, run_dir / "marked.png", point)
                record.update(
                    status="ok",
                    action=None if action is None else action.model_dump(mode="json"),
                    predicted_point_px=list(point),
                    zoom_trace=trace,
                    provider_metadata=metadata,
                    marker={
                        "tool": "OpenCV",
                        "color_bgr": [0, 255, 0],
                        "size_px": [10, 10],
                    },
                )
            except Exception as exc:
                record.update(
                    status="error",
                    error=clean_error(exc),
                    traceback=traceback.format_exc(limit=8),
                )
            record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
            write_json(result_path, record)
            completed += 1
            write_json(
                args.root / args.progress_file,
                {
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "completed": completed,
                    "total": total,
                    "current_case": case["folder"],
                    "current_run": run_name,
                },
            )
            print(
                f"[{completed}/{total}] {case['folder']}/{run_name} "
                f"{record['status']} ocr_direct="
                f"{args.direct_ocr and len(matches) == 1}",
                flush=True,
            )

    write_json(
        args.root / args.progress_file,
        {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "completed": completed,
            "total": total,
            "status": "complete",
        },
    )


if __name__ == "__main__":
    main()
