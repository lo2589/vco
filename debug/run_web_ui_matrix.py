#!/usr/bin/env python3
"""Run a resumable UI-localization matrix and save results without scoring them."""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import cv2
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vco.adaptive_zoom import AdaptiveZoomProvider
from vco.executor import resolve_action
from vco.geometry import GridMapper
from vco.grid import render_numbered_grid
from vco.models import GridSpec, Region
from vco.providers import GLMProvider, MiniMaxProvider, OllamaProvider


DEFAULT_SETTINGS_DIR = Path(
    "/Users/a1/Workspace/PROJECTSPACE/para-llm-for-vscode-main/"
    "paper_bentch/data"
)


@dataclass(frozen=True)
class Configuration:
    mode: str
    grid_n: int
    levels: int

    @property
    def name(self) -> str:
        return f"{self.mode}__n{self.grid_n:02d}"


def configurations() -> list[Configuration]:
    one_stage = [Configuration("single", n, 1) for n in range(8, 31)]
    zoom = [
        Configuration(f"zoom{levels}", n, levels)
        for levels in (2, 3)
        for n in (4, 5, 6)
    ]
    return one_stage + zoom


def read_settings(path: Path) -> dict:
    settings = json.loads(path.read_text(encoding="utf-8"))
    if not settings.get("api_key"):
        raise ValueError(f"missing top-level api_key in {path.name}")
    return settings


def build_providers(settings_dir: Path, selected: set[str]):
    providers = {}
    if "minicpm" in selected:
        providers["minicpm-local-grid"] = OllamaProvider(
            "minicpm-v4.6:latest",
            base_url="http://127.0.0.1:11434",
            timeout=180,
            image_mode="grid",
        )
    if "gemma4" in selected:
        providers["gemma4-local-grid"] = OllamaProvider(
            "gemma4:latest",
            base_url="http://127.0.0.1:11434",
            timeout=300,
            image_mode="grid",
        )
    if "deepseek" in selected:
        settings = read_settings(settings_dir / "provider_settings.deepseek.json")
        providers["deepseek"] = GLMProvider(
            settings.get("model", "deepseek-chat"),
            api_key=settings["api_key"],
            base_url=settings.get("base_url", "https://api.deepseek.com"),
            timeout=60,
            image_mode="both",
            thinking="disabled",
        )
    if "glm" in selected:
        settings = read_settings(settings_dir / "provider_settings.glm.json")
        providers["glm"] = GLMProvider(
            "glm-4.6v",
            api_key=settings["api_key"],
            base_url=settings.get("base_url", "https://open.bigmodel.cn/api/paas/v4/"),
            timeout=150,
            image_mode="both",
            thinking="disabled",
        )
    if "minimax" in selected:
        settings = read_settings(settings_dir / "provider_settings.minimax.json")
        providers["minimax"] = MiniMaxProvider(
            "MiniMax-M3",
            api_key=settings["api_key"],
            base_url="https://api.minimaxi.com/anthropic",
            timeout=90,
            service_tier="priority",
            image_detail="default",
            image_mode="both",
        )
    return providers


def write_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def clean_error(exc: Exception) -> str:
    message = str(exc).replace("\n", " ")
    return f"{type(exc).__name__}: {message[:4000]}"


def mark_with_cv(clean_path: Path, destination: Path, point: tuple[int, int]) -> None:
    image = cv2.imread(str(clean_path), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"OpenCV could not read {clean_path}")
    x, y = point
    left = max(0, x - 5)
    top = max(0, y - 5)
    right = min(image.shape[1] - 1, left + 9)
    bottom = min(image.shape[0] - 1, top + 9)
    left = max(0, right - 9)
    top = max(0, bottom - 9)
    cv2.rectangle(image, (left, top), (right, bottom), (0, 255, 0), thickness=-1)
    if not cv2.imwrite(str(destination), image):
        raise RuntimeError(f"OpenCV could not write {destination}")


def resolve_prediction(action, clean: Image.Image, grid: GridSpec):
    if action.type != "click":
        raise RuntimeError(f"expected click, got {action.type}")
    mapper = GridMapper(Region(width=clean.width, height=clean.height), grid)
    resolved = resolve_action(action, mapper)
    return tuple(resolved.start)


def call_configuration(
    provider,
    config: Configuration,
    task: str,
    clean: Image.Image,
    *,
    gridded_override: Image.Image | None = None,
):
    grid = GridSpec(rows=config.grid_n, cols=config.grid_n)
    gridded = gridded_override or render_numbered_grid(clean, grid)
    if config.levels == 1:
        action = provider.choose_action(
            task=task,
            clean=clean,
            gridded=gridded,
            grid=grid,
            step=1,
            force_click=True,
        )
        trace = None
        metadata = getattr(provider, "last_metadata", None)
    else:
        zoom = AdaptiveZoomProvider(
            provider,
            max_levels=config.levels,
            span_cells=2.0,
            force_initial_click=True,
            always_refine=True,
        )
        action = zoom.choose_action(
            task=task,
            clean=clean,
            gridded=gridded,
            grid=grid,
            step=1,
        )
        trace = zoom.last_trace.as_dict() if zoom.last_trace else None
        metadata = zoom.last_metadata
    point = resolve_prediction(action, clean, grid)
    return action, point, trace, metadata


def update_gallery(
    root: Path, manifest: list[dict], filename: str = "index.html"
) -> None:
    cards = []
    counts = {"ok": 0, "error": 0, "skipped": 0}
    for case in manifest:
        case_dir = root / case["folder"]
        runs = []
        for result_path in sorted(case_dir.glob("*__*__n*/result.json")):
            try:
                record = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            status = record.get("status", "error")
            counts[status] = counts.get(status, 0) + 1
            marked = result_path.parent / "marked.png"
            picture = (
                f'<img loading="lazy" src="{html.escape(marked.relative_to(root).as_posix())}">'
                if marked.exists()
                else '<div class="empty">no point</div>'
            )
            runs.append(
                '<article class="run">'
                f'<h3>{html.escape(result_path.parent.name)}</h3>{picture}'
                f'<p class="{status}">{html.escape(status)}</p>'
                f'<a href="{html.escape(result_path.relative_to(root).as_posix())}">JSON</a>'
                "</article>"
            )
        source = (case_dir / "clean.png").relative_to(root).as_posix()
        cards.append(
            '<section class="case">'
            f'<h2>{html.escape(case["folder"])} — {html.escape(case["task"])}</h2>'
            f'<p><a href="{html.escape((case_dir / "case.json").relative_to(root).as_posix())}">case JSON</a></p>'
            f'<img class="source" loading="lazy" src="{html.escape(source)}">'
            f'<div class="runs">{"".join(runs)}</div></section>'
        )
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="20"><title>VCO web UI matrix</title>
<style>
body{{font:14px system-ui;margin:20px;background:#101216;color:#e7eaf0}}
a{{color:#8ab4ff}} h1,h2,h3{{font-weight:600}} .stats{{position:sticky;top:0;background:#171a21;padding:10px;z-index:2}}
.case{{border-top:1px solid #3a3f4b;padding:18px 0}} .source{{max-width:560px;max-height:360px;object-fit:contain;background:#fff}}
.runs{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px;margin-top:14px}}
.run{{background:#1a1e26;padding:10px;border-radius:8px}} .run img{{width:100%;height:150px;object-fit:contain;background:#fff}}
.empty{{height:150px;display:grid;place-items:center;background:#252a34;color:#999}}
.ok{{color:#5ee47b}} .error{{color:#ff7676}} .skipped{{color:#f8cf63}}
</style></head><body>
<div class="stats"><h1>VCO web UI matrix</h1><p>auto refresh 20s · ok {counts.get('ok',0)} · error {counts.get('error',0)} · skipped {counts.get('skipped',0)}</p></div>
{"".join(cards)}</body></html>"""
    temporary = root / f"{filename}.{os.getpid()}.tmp"
    temporary.write_text(document, encoding="utf-8")
    temporary.replace(root / filename)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("cache/web-ui-matrix"))
    parser.add_argument("--settings-dir", type=Path, default=DEFAULT_SETTINGS_DIR)
    parser.add_argument(
        "--providers",
        default="deepseek,glm,minimax",
        help="comma-separated: deepseek,glm,minimax,minicpm,gemma4",
    )
    parser.add_argument("--limit-cases", type=int)
    parser.add_argument("--limit-configs", type=int)
    parser.add_argument("--progress-file", default="progress.json")
    parser.add_argument("--gallery-file", default="index.html")
    args = parser.parse_args()

    selected = {item.strip() for item in args.providers.split(",") if item.strip()}
    providers = build_providers(args.settings_dir, selected)
    manifest = json.loads((args.root / "manifest.json").read_text(encoding="utf-8"))
    if args.limit_cases:
        manifest = manifest[: args.limit_cases]
    configs = configurations()
    if args.limit_configs:
        configs = configs[: args.limit_configs]
    total = len(manifest) * len(configs) * len(providers)
    completed = 0
    disabled: dict[str, str] = {}
    started_at = datetime.now(timezone.utc).isoformat()
    progress_path = args.root / args.progress_file
    update_gallery(args.root, manifest, args.gallery_file)

    for case in manifest:
        case_dir = args.root / case["folder"]
        clean_path = case_dir / case.get("filename", "clean.png")
        with Image.open(clean_path) as source:
            clean = source.convert("RGB")
        for provider_name, provider in providers.items():
            for config in configs:
                run_name = f"{provider_name}__{config.name}"
                run_dir = case_dir / run_name
                result_path = run_dir / "result.json"
                if result_path.exists():
                    completed += 1
                    continue
                run_dir.mkdir(parents=True, exist_ok=True)
                record = {
                    "case_id": case["id"],
                    "task": case["task"],
                    "provider": provider_name,
                    "model": provider.model,
                    "mode": config.mode,
                    "grid": {"rows": config.grid_n, "cols": config.grid_n},
                    "levels": config.levels,
                    "source_image": "../clean.png",
                    "target_bbox_xyxy": case.get("target_bbox_xyxy"),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
                begun = time.perf_counter()
                if provider_name in disabled:
                    record.update(
                        status="skipped",
                        error=disabled[provider_name],
                        note="provider disabled after its first benchmark error",
                    )
                else:
                    try:
                        action, point, trace, metadata = call_configuration(
                            provider, config, case["task"], clean
                        )
                        mark_with_cv(clean_path, run_dir / "marked.png", point)
                        record.update(
                            status="ok",
                            action=action.model_dump(mode="json"),
                            predicted_point_px=list(point),
                            zoom_trace=trace,
                            provider_metadata=metadata,
                            marker={"tool": "OpenCV", "color_bgr": [0, 255, 0], "size_px": [10, 10]},
                        )
                    except Exception as exc:
                        error = clean_error(exc)
                        record.update(
                            status="error",
                            error=error,
                            traceback=traceback.format_exc(limit=8),
                        )
                        # The configured official DeepSeek text model currently has
                        # no image input. Probe once so a future vision-capable model
                        # can work automatically, then avoid hundreds of identical 4xxs.
                        if provider_name == "deepseek":
                            disabled[provider_name] = error
                record["elapsed_seconds"] = round(time.perf_counter() - begun, 3)
                record["finished_at"] = datetime.now(timezone.utc).isoformat()
                write_json(result_path, record)
                completed += 1
                write_json(
                    progress_path,
                    {
                        "pid": os.getpid(),
                        "started_at": started_at,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "completed": completed,
                        "total": total,
                        "current_case": case["folder"],
                        "current_run": run_name,
                        "disabled_providers": disabled,
                    },
                )
                update_gallery(args.root, manifest, args.gallery_file)
                print(
                    f"[{completed}/{total}] {case['folder']}/{run_name} "
                    f"{record['status']} {record['elapsed_seconds']}s",
                    flush=True,
                )

    write_json(
        progress_path,
        {
            "pid": os.getpid(),
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "completed": completed,
            "total": total,
            "status": "complete",
            "disabled_providers": disabled,
        },
    )
    update_gallery(args.root, manifest, args.gallery_file)


if __name__ == "__main__":
    main()
