#!/usr/bin/env python3
"""Diagnose where fixed adaptive zoom loses a human-labelled target."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


PROVIDERS = ("minicpm-local-grid", "gemma4-local-grid", "glm", "minimax")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def intersects(first, second) -> bool:
    return not (
        first[2] <= second[0]
        or first[0] >= second[2]
        or first[3] <= second[1]
        or first[1] >= second[3]
    )


def contains(box, point) -> bool:
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def decision_cell(decision: dict) -> int | None:
    if decision.get("type") == "zoom":
        return decision.get("cell")
    return (decision.get("target") or {}).get("cell")


def cell_box(view_box, grid_n: int, cell: int):
    row, col = divmod(cell - 1, grid_n)
    left, top, right, bottom = view_box
    width, height = right - left, bottom - top
    return (
        left + col * width / grid_n,
        top + row * height / grid_n,
        left + (col + 1) * width / grid_n,
        top + (row + 1) * height / grid_n,
    )


def expected_cell(view_box, grid_n: int, point) -> int | None:
    if not contains(view_box, point):
        return None
    left, top, right, bottom = view_box
    col = min(grid_n - 1, max(0, int((point[0] - left) * grid_n / (right - left))))
    row = min(grid_n - 1, max(0, int((point[1] - top) * grid_n / (bottom - top))))
    return row * grid_n + col + 1


def distance_to_box(point, box) -> float:
    dx = max(box[0] - point[0], 0.0, point[0] - box[2])
    dy = max(box[1] - point[1], 0.0, point[1] - box[3])
    return math.hypot(dx, dy)


def percent(value: int, total: int) -> float | None:
    return round(value / total, 4) if total else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("cache/web-ui-matrix"))
    parser.add_argument(
        "--annotations", type=Path, default=Path("example/web_ui_ground_truth_aio.json")
    )
    args = parser.parse_args()
    truth = {
        Path(row["filename"]).parent.name: row["instances"][0]["bbox"]
        for row in read_jsonl(args.annotations)
    }
    buckets = defaultdict(list)
    details = []

    for result_path in sorted(args.root.glob("case-*/*/result.json")):
        result = json.loads(result_path.read_text())
        provider = result.get("provider")
        mode = result.get("mode")
        if provider not in PROVIDERS or mode not in {"zoom2", "zoom3"}:
            continue
        case = result_path.parents[1].name
        bbox = truth[case]
        target_center = ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
        grid_n = int(result["grid"]["rows"])
        record = {
            "provider": provider,
            "mode": mode,
            "grid_n": grid_n,
            "case": case,
            "status": result.get("status"),
            "first_cell_contains_center": False,
            "first_cell_intersects_target": False,
            "all_crops_retain_center": False,
            "all_crops_intersect_target": False,
            "final_cell_contains_center": False,
            "final_cell_intersects_target": False,
            "selected_cell_center_hit": False,
            "strict_hit": False,
            "within_20px": False,
            "within_50px": False,
            "miss_distance_px": None,
        }
        if result.get("status") != "ok":
            buckets[(provider, mode, grid_n)].append(record)
            details.append(record)
            continue
        levels = (result.get("zoom_trace") or {}).get("levels") or []
        if not levels:
            buckets[(provider, mode, grid_n)].append(record)
            details.append(record)
            continue
        first = levels[0]
        first_cell = decision_cell(first["decision"])
        if first_cell:
            first_box = cell_box(first["view_box_image_pixels"], grid_n, first_cell)
            record["first_cell_contains_center"] = contains(first_box, target_center)
            record["first_cell_intersects_target"] = intersects(first_box, bbox)
        intermediate = [level for level in levels[:-1] if level.get("next_view_box_image_pixels")]
        record["all_crops_retain_center"] = all(
            contains(level["next_view_box_image_pixels"], target_center)
            for level in intermediate
        )
        record["all_crops_intersect_target"] = all(
            intersects(level["next_view_box_image_pixels"], bbox)
            for level in intermediate
        )
        final = levels[-1]
        final_cell = decision_cell(final["decision"])
        if final_cell:
            final_box = cell_box(final["view_box_image_pixels"], grid_n, final_cell)
            record["final_cell_contains_center"] = contains(final_box, target_center)
            record["final_cell_intersects_target"] = intersects(final_box, bbox)
            cell_center = (
                (final_box[0] + final_box[2]) / 2,
                (final_box[1] + final_box[3]) / 2,
            )
            record["selected_cell_center_hit"] = contains(bbox, cell_center)
        point = result.get("predicted_point_px")
        if point:
            miss = distance_to_box(point, bbox)
            record["miss_distance_px"] = round(miss, 3)
            record["strict_hit"] = miss == 0
            record["within_20px"] = miss <= 20
            record["within_50px"] = miss <= 50
        buckets[(provider, mode, grid_n)].append(record)
        details.append(record)

    summaries = []
    fields = (
        "first_cell_contains_center",
        "first_cell_intersects_target",
        "all_crops_retain_center",
        "all_crops_intersect_target",
        "final_cell_contains_center",
        "final_cell_intersects_target",
        "selected_cell_center_hit",
        "strict_hit",
        "within_20px",
        "within_50px",
    )
    for (provider, mode, grid_n), rows in buckets.items():
        total = len(rows)
        ok = sum(row["status"] == "ok" for row in rows)
        item = {
            "provider": provider,
            "mode": mode,
            "grid_n": grid_n,
            "total": total,
            "successful_results": ok,
            "errors": total - ok,
        }
        for field in fields:
            count = sum(bool(row[field]) for row in rows)
            item[field] = count
            item[field + "_rate"] = percent(count, total)
        summaries.append(item)
    summaries.sort(key=lambda item: (item["provider"], item["mode"], item["grid_n"]))
    output = {"summaries": summaries, "details": details}
    (args.root / "zoom-diagnosis.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    )

    aggregate = defaultdict(list)
    for row in details:
        aggregate[(row["provider"], row["mode"])].append(row)
    lines = [
        "# Zoom failure diagnosis",
        "",
        "All rates count API/parse errors as failures.",
        "",
        "| Provider | Mode | First cell contains target center | Crops retain target center | Final cell contains target center | Strict hit | ≤20px | ≤50px |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for (provider, mode), rows in sorted(aggregate.items()):
        total = len(rows)
        values = [
            sum(row[field] for row in rows)
            for field in (
                "first_cell_contains_center",
                "all_crops_retain_center",
                "final_cell_contains_center",
                "strict_hit",
                "within_20px",
                "within_50px",
            )
        ]
        lines.append(
            f"| {provider} | {mode} | "
            + " | ".join(f"{value}/{total} ({value/total:.1%})" for value in values)
            + " |"
        )
    (args.root / "ZOOM_DIAGNOSIS.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
