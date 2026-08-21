"""Screenshot -> grid -> model -> local mapping -> action closed loop."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .executor import ActionExecutor, resolve_action
from .geometry import GridMapper
from .grid import render_numbered_grid
from .models import GridSpec, Region, points_in_action
from .zoom import ZoomProvider


@dataclass
class LoopResult:
    status: str
    steps: int
    reason: str
    run_dir: Path


class ComputerUseLoop:
    def __init__(
        self,
        *,
        capture,
        provider,
        executor: ActionExecutor,
        region: Region,
        grid: GridSpec,
        artifact_root: Path,
        settle_seconds: float = 0.7,
    ):
        self.capture = capture
        self.provider = provider
        self.executor = executor
        self.region = region
        self.grid = grid
        self.artifact_root = artifact_root
        self.settle_seconds = settle_seconds
        self.mapper = GridMapper(region, grid)

    def run(self, task: str, *, max_steps: int = 12) -> LoopResult:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        run_dir = self.artifact_root / stamp
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "mapping.json").write_text(
            json.dumps(self.mapper.mapping_table(), indent=2),
            encoding="utf-8",
        )
        (run_dir / "task.txt").write_text(task + "\n", encoding="utf-8")

        for step in range(1, max_steps + 1):
            frame = self.capture.capture(self.region)
            gridded = render_numbered_grid(frame.image, self.grid)
            clean_path = run_dir / f"step-{step:03d}-clean.png"
            grid_path = run_dir / f"step-{step:03d}-grid.png"
            frame.image.save(clean_path)
            gridded.save(grid_path)

            action = self.provider.choose_action(
                task=task,
                clean=frame.image,
                gridded=gridded,
                grid=self.grid,
                step=step,
            )
            for point in points_in_action(action):
                self.mapper.row_col(point.cell)

            resolved = resolve_action(action, self.mapper)
            zoom_trace = None
            if isinstance(self.provider, ZoomProvider) and self.provider.last_trace:
                zoom_trace = self.provider.last_trace.as_dict()
                if self.provider.last_zoom_clean is not None:
                    self.provider.last_zoom_clean.save(
                        run_dir / f"step-{step:03d}-zoom-clean.png"
                    )
                if self.provider.last_zoom_grid is not None:
                    self.provider.last_zoom_grid.save(
                        run_dir / f"step-{step:03d}-zoom-grid.png"
                    )
            elif getattr(self.provider, "last_trace", None) is not None:
                zoom_trace = self.provider.last_trace.as_dict()
                for level, (zoom_clean, zoom_grid) in enumerate(
                    getattr(self.provider, "last_zoom_images", []), start=2
                ):
                    zoom_clean.save(
                        run_dir / f"step-{step:03d}-zoom-{level:02d}-clean.png"
                    )
                    zoom_grid.save(
                        run_dir / f"step-{step:03d}-zoom-{level:02d}-grid.png"
                    )
            record = {
                "model_action": action.model_dump(mode="json"),
                "provider_metadata": getattr(self.provider, "last_metadata", None),
                "zoom_trace": zoom_trace,
                "resolved_local_action": (
                    None
                    if resolved is None
                    else {
                        "type": resolved.type,
                        "start": resolved.start,
                        "end": resolved.end,
                        "duration": resolved.duration,
                        "button": resolved.button,
                    }
                ),
            }
            (run_dir / f"step-{step:03d}-action.json").write_text(
                json.dumps(record, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            if action.type == "done":
                return LoopResult("done", step, action.reason, run_dir)

            assert resolved is not None
            self.executor.execute(resolved)
            if self.settle_seconds:
                time.sleep(self.settle_seconds)

        return LoopResult(
            "max_steps",
            max_steps,
            f"stopped after max_steps={max_steps}",
            run_dir,
        )
