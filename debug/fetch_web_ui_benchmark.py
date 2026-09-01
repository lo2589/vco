#!/usr/bin/env python3
"""Download ten public UI-grounding cases without rendering or inspecting them."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen

from PIL import Image


DATASET = "ai-multiple/aim-ui-grounding"
DATASET_PAGE = f"https://huggingface.co/datasets/{DATASET}"
ROWS_URL = (
    "https://datasets-server.huggingface.co/rows"
    f"?dataset={DATASET.replace('/', '%2F')}&config=default&split=train"
    "&offset=0&length=10"
)


def _get_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "vco/0.1.5"})
    with urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def _download(url: str, destination: Path) -> None:
    request = Request(url, headers={"User-Agent": "vco/0.1.5"})
    with urlopen(request, timeout=120) as response:
        destination.write_bytes(response.read())


def _slug(value: object) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "ui").lower()).strip("-")
    return text[:36] or "ui"


def _first(row: dict, *names: str):
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def _image_url(row: dict) -> str:
    value = _first(row, "image", "screenshot", "screen")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for name in ("src", "url", "path"):
            candidate = value.get(name)
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                return candidate
    raise ValueError(f"row has no downloadable image URL; keys={sorted(row)}")


def _bbox(row: dict):
    value = _first(row, "target_bbox_xyxy", "bbox", "target_bbox")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if isinstance(value, (list, tuple)) and len(value) == 4:
        return [float(item) for item in value]
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("cache/web-ui-matrix"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    payload = _get_json(ROWS_URL)
    rows = payload.get("rows", [])
    if len(rows) < 10:
        raise RuntimeError(f"dataset server returned only {len(rows)} rows")

    manifest = []
    for index, item in enumerate(rows[:10], start=1):
        row = item.get("row", item)
        application = _first(row, "application", "domain", "platform", "os") or "ui"
        case_name = f"case-{index:02d}-{_slug(application)}"
        case_dir = args.output / case_name
        case_dir.mkdir(parents=True, exist_ok=True)
        clean_path = case_dir / "clean.png"
        _download(_image_url(row), clean_path)
        with Image.open(clean_path) as source:
            source.verify()
        with Image.open(clean_path) as source:
            size = list(source.size)
            if source.format != "PNG":
                source.convert("RGB").save(clean_path, format="PNG", optimize=True)

        task = _first(row, "instruction", "task", "query", "target_description")
        if not task:
            raise ValueError(f"case {index} has no instruction")
        record = {
            "id": f"{index:02d}",
            "folder": case_name,
            "filename": "clean.png",
            "task": str(task),
            "target_bbox_xyxy": _bbox(row),
            "image_size": size,
            "application": application,
            "domain": row.get("domain"),
            "os": row.get("os"),
            "source_dataset": DATASET,
            "source_page": DATASET_PAGE,
            "source_row_index": item.get("row_idx", index - 1),
            "license": "CC BY-NC-ND 4.0",
        }
        (case_dir / "case.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest.append(record)
        print(f"[{index:02d}/10] {case_name} {size[0]}x{size[1]}", flush=True)

    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"saved {len(manifest)} cases to {args.output}", flush=True)


if __name__ == "__main__":
    main()
