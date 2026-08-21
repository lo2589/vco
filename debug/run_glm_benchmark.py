"""Benchmark one-shot GLM localization on the existing 10 UI cases."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vco.executor import resolve_action
from vco.geometry import GridMapper
from vco.grid import render_numbered_grid
from vco.models import GridSpec, Region
from vco.providers import GLMProvider


def point_in_box(point, box):
    x, y = point
    left, top, right, bottom = box
    return left <= x <= right and top <= y <= bottom


def miss_distance(point, box):
    x, y = point
    left, top, right, bottom = box
    dx = max(left - x, 0, x - right)
    dy = max(top - y, 0, y - bottom)
    return round((dx * dx + dy * dy) ** 0.5, 2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=Path("debug/cases"))
    parser.add_argument(
        "--output", type=Path, default=Path("debug/results-glm-4.6v-16x16")
    )
    parser.add_argument("--model", default="glm-4.6v")
    parser.add_argument("--image-mode", choices=("both", "clean", "grid"), default="both")
    args = parser.parse_args()

    settings = json.loads(args.settings.read_text(encoding="utf-8"))
    provider = GLMProvider(
        args.model,
        api_key=settings["api_key"],
        base_url=settings.get("base_url", "https://open.bigmodel.cn/api/paas/v4/"),
        timeout=120,
        image_mode=args.image_mode,
        thinking="disabled",
    )
    cases = json.loads((args.cases / "manifest.json").read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=True)
    grid = GridSpec(rows=16, cols=16)
    results = []

    for index, case in enumerate(cases, start=1):
        case_dir = args.output / f"case-{case['id']}"
        case_dir.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        record = {
            "id": case["id"],
            "task": case["task"],
            "target_bbox": case["target_bbox"],
        }
        try:
            clean = Image.open(args.cases / case["filename"]).convert("RGB")
            gridded = render_numbered_grid(clean, grid)
            gridded.save(case_dir / "grid-16x16.png")
            action = provider.choose_action(
                task=case["task"],
                clean=clean,
                gridded=gridded,
                grid=grid,
                step=1,
                force_click=True,
            )
            resolved = resolve_action(
                action, GridMapper(Region(width=clean.width, height=clean.height), grid)
            )
            point = resolved.start
            hit = point_in_box(point, case["target_bbox"])
            record.update(
                {
                    "grid_point": action.target.model_dump(mode="json"),
                    "predicted_point": list(point),
                    "hit": hit,
                    "miss_distance_px": miss_distance(point, case["target_bbox"]),
                    "provider_metadata": provider.last_metadata,
                }
            )
        except Exception as exc:
            record.update(
                {
                    "predicted_point": None,
                    "hit": False,
                    "miss_distance_px": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        (case_dir / "result.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        results.append(record)
        print(
            f"[{index:02d}/{len(cases):02d}] case-{case['id']} "
            f"{'HIT' if record['hit'] else 'MISS'} "
            f"point={record['predicted_point']} elapsed={record['elapsed_seconds']}s",
            flush=True,
        )

    hits = sum(item["hit"] for item in results)
    total = len(results)
    summary = {
        "model": args.model,
        "configuration": {
            "grid": "16x16",
            "stages": 1,
            "image_mode": args.image_mode,
            "thinking": "disabled",
        },
        "hits": hits,
        "total": total,
        "accuracy": hits / total,
        "mean_elapsed_seconds": round(
            sum(item["elapsed_seconds"] for item in results) / total, 3
        ),
        "results": results,
    }
    (args.output / "results.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
