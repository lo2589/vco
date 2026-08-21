"""Run the local zoom provider over generated cases and score click accuracy."""

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
from vco.providers import OllamaProvider
from vco.zoom import ZoomProvider


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
    parser.add_argument("--cases", type=Path, default=Path("debug/cases"))
    parser.add_argument("--output", type=Path, default=Path("debug/results"))
    parser.add_argument("--model", default="minicpm-v4.6:latest")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--center-delta", action="store_true")
    args = parser.parse_args()

    cases = json.loads((args.cases / "manifest.json").read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=True)
    grid = GridSpec(rows=4, cols=4)
    base = OllamaProvider(
        args.model,
        base_url=args.ollama_url,
        timeout=args.timeout,
        image_mode="grid",
    )
    provider = ZoomProvider(
        base,
        force_initial_click=True,
        use_center_delta=args.center_delta,
    )
    results = []

    for index, case in enumerate(cases, start=1):
        case_dir = args.output / f"case-{case['id']}"
        case_dir.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        result = {
            "id": case["id"],
            "task": case["task"],
            "target_bbox": case["target_bbox"],
        }
        try:
            with Image.open(args.cases / case["filename"]) as source:
                clean = source.convert("RGB")
            coarse_grid = render_numbered_grid(clean, grid)
            coarse_grid.save(case_dir / "coarse-grid.png")
            action = provider.choose_action(
                task=case["task"],
                clean=clean,
                gridded=coarse_grid,
                grid=grid,
                step=1,
            )
            mapper = GridMapper(
                Region(x=0, y=0, width=clean.width, height=clean.height), grid
            )
            resolved = resolve_action(action, mapper)
            if resolved is None:
                raise RuntimeError("localization unexpectedly returned done")
            point = resolved.start
            hit = point_in_box(point, case["target_bbox"])
            result.update(
                {
                    "predicted_point": list(point),
                    "hit": hit,
                    "miss_distance_px": miss_distance(point, case["target_bbox"]),
                    "zoom_trace": provider.last_trace.as_dict(),
                }
            )
            if provider.last_zoom_clean is not None:
                provider.last_zoom_clean.save(case_dir / "zoom-clean.png")
            if provider.last_zoom_grid is not None:
                provider.last_zoom_grid.save(case_dir / "zoom-grid.png")
        except Exception as exc:
            result.update(
                {
                    "predicted_point": None,
                    "hit": False,
                    "miss_distance_px": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        (case_dir / "result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        results.append(result)
        status = "HIT" if result["hit"] else "MISS"
        print(
            f"[{index:02d}/{len(cases):02d}] case-{case['id']} {status} "
            f"point={result['predicted_point']} elapsed={result['elapsed_seconds']}s",
            flush=True,
        )

    hits = sum(result["hit"] for result in results)
    total = len(results)
    elapsed = sum(result["elapsed_seconds"] for result in results)
    within_20px = sum(
        result["hit"]
        or (
            result.get("miss_distance_px") is not None
            and result["miss_distance_px"] <= 20
        )
        for result in results
    )
    summary = {
        "model": args.model,
        "configuration": {
            "coarse_grid": "4x4",
            "crop_span": "2x2 coarse cells",
            "fine_grid": "4x4",
            "image_mode": "grid",
            "final_point": (
                "model signed pixel delta from number center"
                if args.center_delta
                else "fine cell center"
            ),
        },
        "hits": hits,
        "total": total,
        "accuracy": hits / total if total else 0,
        "within_20px": within_20px,
        "within_20px_rate": within_20px / total if total else 0,
        "total_elapsed_seconds": round(elapsed, 3),
        "mean_elapsed_seconds": round(elapsed / total, 3) if total else 0,
        "results": results,
    }
    (args.output / "results.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Debug benchmark result",
        "",
        f"- Model: `{args.model}`",
        f"- Accuracy: **{hits}/{total} ({summary['accuracy']:.1%})**",
        f"- Mean latency: {summary['mean_elapsed_seconds']}s per image",
        f"- Diagnostic 20px-expanded-box score: {within_20px}/{total} "
        f"({summary['within_20px_rate']:.1%}); this is not counted as a real click success.",
        "",
        "| Case | Task | Point | Hit | Time |",
        "|---|---|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result['id']} | {result['task']} | "
            f"`{result['predicted_point']}` | "
            f"{'yes' if result['hit'] else 'no'} | "
            f"{result['elapsed_seconds']}s |"
        )
    misses = [result for result in results if not result["hit"]]
    if misses:
        lines.extend(
            [
                "",
                "## Failure analysis",
                "",
                "| Case | Target box | Point | Outside distance | Coarse/Fine cell |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for result in misses:
            trace = result.get("zoom_trace") or {}
            coarse = ((trace.get("coarse_action") or {}).get("target") or {}).get(
                "cell"
            )
            fine = ((trace.get("fine_action") or {}).get("target") or {}).get("cell")
            lines.append(
                f"| {result['id']} | `{result['target_bbox']}` | "
                f"`{result['predicted_point']}` | {result['miss_distance_px']}px | "
                f"`{coarse} / {fine}` |"
            )
    (args.output / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"accuracy={hits}/{total} ({summary['accuracy']:.1%})", flush=True)


if __name__ == "__main__":
    main()
