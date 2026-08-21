"""Benchmark one-shot MiniMax-M3 localization on a dense numbered grid."""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vco.geometry import GridMapper
from vco.grid import render_numbered_grid
from vco.models import GridPoint, GridSpec, Region


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


def extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"response has no JSON object: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def ask_minimax(
    *, api_key: str, image: Image.Image, task: str, service_tier: str, detail: str
):
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    prompt = (
        f"Task: {task}\n"
        "The image is a numbered 16x16 grid over a 1000x600 UI. "
        "Cells are numbered 1-256 in row-major order. Each cell is 62.5 pixels "
        "wide and 37.5 pixels high. Locate the center of the requested control. "
        "Return only compact JSON: "
        '{"cell":integer,"offset_x":number,"offset_y":number}. '
        "Offsets range from 0 to 1 inside the cell, measured from left/top."
    )
    payload = {
        "model": "MiniMax-M3",
        "max_tokens": 60,
        "temperature": 0,
        "service_tier": service_tier,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": encoded,
                        },
                        "detail": detail,
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    request = urllib.request.Request(
        "https://api.minimaxi.com/anthropic/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = json.loads(response.read())
    text = "".join(
        block.get("text", "")
        for block in body.get("content", [])
        if block.get("type") == "text"
    )
    return extract_json(text), body.get("usage"), text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=Path("debug/cases"))
    parser.add_argument("--output", type=Path, default=Path("debug/results-minimax-m3-16x16"))
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--service-tier", choices=("standard", "priority"), default="priority")
    parser.add_argument("--detail", choices=("low", "default", "high"), default="default")
    args = parser.parse_args()

    settings = json.loads(args.settings.read_text(encoding="utf-8"))
    api_key = settings["api_key"]
    cases = json.loads((args.cases / "manifest.json").read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=True)
    grid = GridSpec(rows=16, cols=16)
    results = []

    for index, case in enumerate(cases, start=1):
        case_dir = args.output / f"case-{case['id']}"
        case_dir.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        result = {"id": case["id"], "task": case["task"], "target_bbox": case["target_bbox"]}
        try:
            with Image.open(args.cases / case["filename"]) as source:
                clean = source.convert("RGB")
            gridded = render_numbered_grid(clean, grid)
            gridded.save(case_dir / "grid-16x16.png")
            raw_point, usage, raw_text = ask_minimax(
                api_key=api_key,
                image=gridded,
                task=case["task"],
                service_tier=args.service_tier,
                detail=args.detail,
            )
            point = GridPoint.model_validate(raw_point)
            mapper = GridMapper(Region(width=clean.width, height=clean.height), grid)
            predicted = mapper.to_screen(point)
            hit = point_in_box(predicted, case["target_bbox"])
            result.update(
                {
                    "grid_point": point.model_dump(mode="json"),
                    "predicted_point": list(predicted),
                    "hit": hit,
                    "miss_distance_px": miss_distance(predicted, case["target_bbox"]),
                    "usage": usage,
                    "raw_text": raw_text,
                }
            )
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
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        results.append(result)
        status = "HIT" if result["hit"] else "MISS"
        print(
            f"[{index:02d}/{len(cases):02d}] case-{case['id']} {status} "
            f"point={result['predicted_point']} elapsed={result['elapsed_seconds']}s",
            flush=True,
        )

    hits = sum(item["hit"] for item in results)
    elapsed = sum(item["elapsed_seconds"] for item in results)
    total = len(results)
    summary = {
        "model": "MiniMax-M3",
        "configuration": {
            "grid": "16x16",
            "stages": 1,
            "service_tier": args.service_tier,
            "image_detail": args.detail,
        },
        "hits": hits,
        "total": total,
        "accuracy": hits / total,
        "mean_elapsed_seconds": round(elapsed / total, 3),
        "results": results,
    }
    (args.output / "results.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    lines = [
        "# MiniMax-M3 one-shot 16x16 benchmark",
        "",
        f"- Accuracy: **{hits}/{total} ({hits / total:.1%})**",
        f"- Mean latency: {summary['mean_elapsed_seconds']}s per image",
        f"- Service tier: `{args.service_tier}`",
        f"- Image detail: `{args.detail}`",
        "",
        "| Case | Point | Cell | Hit | Time |",
        "|---|---:|---:|---:|---:|",
    ]
    for item in results:
        cell = (item.get("grid_point") or {}).get("cell")
        lines.append(
            f"| {item['id']} | `{item['predicted_point']}` | {cell} | "
            f"{'yes' if item['hit'] else 'no'} | {item['elapsed_seconds']}s |"
        )
    (args.output / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"accuracy={hits}/{total} ({hits / total:.1%})", flush=True)


if __name__ == "__main__":
    main()
