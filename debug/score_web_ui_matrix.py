#!/usr/bin/env python3
"""Score saved prediction points against human boxes without opening images."""

from __future__ import annotations

import argparse
import html
import json
import math
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def distance_to_box(point, box) -> float:
    x, y = point
    left, top, right, bottom = box
    dx = max(left - x, 0.0, x - right)
    dy = max(top - y, 0.0, y - bottom)
    return math.hypot(dx, dy)


def center_distance(point, box) -> float:
    x, y = point
    left, top, right, bottom = box
    return math.hypot(x - (left + right) / 2, y - (top + bottom) / 2)


def write_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def score_once(root: Path, annotations: Path) -> dict:
    truth = {}
    for row in read_jsonl(annotations):
        instances = [item for item in row.get("instances", []) if not item.get("is_ignored")]
        if len(instances) != 1:
            raise ValueError(f"expected one active box for {row.get('filename')}")
        case_folder = Path(row["filename"]).parent.name
        truth[case_folder] = {
            "bbox": instances[0]["bbox"],
            "note": row.get("note", ""),
        }

    records = []
    status_counts = defaultdict(int)
    for result_path in sorted(root.glob("case-*/*__*__n*/result.json")):
        result = json.loads(result_path.read_text(encoding="utf-8"))
        status_counts[result.get("status", "unknown")] += 1
        point = result.get("predicted_point_px")
        case_folder = result_path.parents[1].name
        if result.get("status") != "ok" or not point or case_folder not in truth:
            continue
        box = truth[case_folder]["bbox"]
        miss = distance_to_box(point, box)
        record = {
            "case": case_folder,
            "run": result_path.parent.name,
            "provider": result["provider"],
            "mode": result["mode"],
            "grid_n": result["grid"]["rows"],
            "levels": result["levels"],
            "predicted_point_px": point,
            "ground_truth_bbox_xyxy": box,
            "hit": miss == 0,
            "miss_distance_px": round(miss, 3),
            "target_center_distance_px": round(center_distance(point, box), 3),
        }
        write_json(result_path.parent / "score.json", record)
        records.append(record)

    groups = defaultdict(list)
    for record in records:
        groups[(record["provider"], record["mode"], record["grid_n"])].append(record)
    summaries = []
    for (provider, mode, grid_n), items in groups.items():
        hits = sum(item["hit"] for item in items)
        summaries.append(
            {
                "provider": provider,
                "mode": mode,
                "grid_n": grid_n,
                "scored": len(items),
                "hits": hits,
                "accuracy": round(hits / len(items), 6),
                "mean_miss_distance_px": round(
                    statistics.fmean(item["miss_distance_px"] for item in items), 3
                ),
                "median_center_distance_px": round(
                    statistics.median(item["target_center_distance_px"] for item in items), 3
                ),
            }
        )
    summaries.sort(
        key=lambda item: (
            -item["accuracy"],
            item["mean_miss_distance_px"],
            item["provider"],
            item["mode"],
            item["grid_n"],
        )
    )
    hits = sum(item["hit"] for item in records)
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "annotation_file": str(annotations),
        "rule": "hit iff predicted point is inside the human bbox; otherwise Euclidean distance to nearest bbox edge",
        "status_counts": dict(status_counts),
        "scored_predictions": len(records),
        "hits": hits,
        "accuracy": round(hits / len(records), 6) if records else None,
        "groups": summaries,
        "records": records,
    }


def write_html(root: Path, scoring: dict) -> None:
    rows = []
    for item in scoring["groups"]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(item['provider'])}</td>"
            f"<td>{html.escape(item['mode'])}</td>"
            f"<td>{item['grid_n']}×{item['grid_n']}</td>"
            f"<td>{item['hits']}/{item['scored']}</td>"
            f"<td>{item['accuracy']:.1%}</td>"
            f"<td>{item['mean_miss_distance_px']:.1f}</td>"
            f"<td>{item['median_center_distance_px']:.1f}</td>"
            "</tr>"
        )
    document = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="20"><title>VCO 实时评分</title>
<style>body{{font:14px system-ui;margin:24px;background:#101216;color:#e8ebf1}}a{{color:#8ab4ff}}
table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #3d4350;padding:7px;text-align:right}}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){{text-align:left}}thead{{position:sticky;top:0;background:#202530}}</style>
</head><body><h1>VCO 实时评分</h1>
<p>已评分 {scoring['scored_predictions']} 个成功预测；命中 {scoring['hits']}；总体 {scoring['accuracy'] or 0:.1%}。每 20 秒刷新。</p>
<p>命中规则：预测点落在人工框内。距离为未命中点到人工框最近边缘的欧氏像素距离。</p>
<p><a href="scoring.json">完整评分 JSON</a> · <a href="index.html">预测图画廊</a></p>
<table><thead><tr><th>模型</th><th>模式</th><th>网格</th><th>命中</th><th>准确率</th><th>平均框外距离</th><th>中心距离中位数</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></body></html>"""
    temporary = root / "scoring.html.tmp"
    temporary.write_text(document, encoding="utf-8")
    temporary.replace(root / "scoring.html")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("cache/web-ui-matrix"))
    parser.add_argument(
        "--annotations",
        type=Path,
        default=Path("example/web_ui_ground_truth_aio.json"),
    )
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=20.0)
    args = parser.parse_args()
    while True:
        scoring = score_once(args.root, args.annotations)
        write_json(args.root / "scoring.json", scoring)
        write_html(args.root, scoring)
        print(
            f"scored={scoring['scored_predictions']} hits={scoring['hits']} "
            f"accuracy={scoring['accuracy']}",
            flush=True,
        )
        if not args.watch:
            break
        progress_path = args.root / "progress.json"
        if progress_path.exists():
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            if progress.get("status") == "complete":
                break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
