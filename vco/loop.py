"""Screenshot -> grid -> model -> local mapping -> action closed loop."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image

from .diff import pixel_similarity
from .events import emit
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


def _action_summary(action) -> str:
    """One-line human-readable action for the monitor timeline and history."""
    if action.type in {"click", "double_click", "right_click"}:
        return f"{action.type} cell={action.target.cell}"
    if action.type == "drag":
        return f"drag cell={action.start.cell} -> cell={action.end.cell}"
    if action.type == "scroll":
        return f"scroll {action.amount:+d} at cell={action.target.cell}"
    if action.type == "type":
        text = action.text if len(action.text) <= 60 else action.text[:57] + "..."
        return f"type {json.dumps(text, ensure_ascii=False)}"
    if action.type == "key":
        return f"key {action.keys}"
    if action.type == "wait":
        return f"wait {action.seconds:g}s"
    return f"done: {action.reason}"


# Above this similarity two frames are "the same screen": cursor blink and
# clock ticks stay under it, a menu opening or text appearing does not.
UNCHANGED_SIMILARITY = 0.998
HISTORY_LIMIT = 12


def _screen_changed(before: Image.Image, after: Image.Image) -> bool:
    size = (min(before.width, 480), min(before.height, 300))
    return (
        pixel_similarity(before.resize(size), after.resize(size))
        < UNCHANGED_SIMILARITY
    )


def _task_with_history(task: str, history: list[str]) -> str:
    """Fold earlier steps into the task text.

    Every provider and wrapper (OCR hints, zoom) already threads ``task``
    through untouched, so this reaches all of them without a new parameter.
    Without it each step is the first step: the model re-clicks a button that
    did nothing, or never says done because it cannot see it already acted.
    """

    if not history:
        return task
    return (
        f"{task}\n\nActions already taken, oldest first (the screenshot shows "
        "the state after the last one):\n"
        + "\n".join(history[-HISTORY_LIMIT:])
        + "\nDo not repeat an action whose screen did not change; try a "
        "different action instead. Return done once the task is visibly complete."
    )


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
        max_unchanged: int = 3,
        on_step=None,
    ):
        self.capture = capture
        self.provider = provider
        self.executor = executor
        self.region = region
        self.grid = grid
        self.artifact_root = artifact_root
        self.settle_seconds = settle_seconds
        # Stop after this many consecutive actions that left the screen as it
        # was: the model is stuck, and more steps only repeat the miss.
        # 0 disables the check.
        self.max_unchanged = max_unchanged
        # Optional ``on_step(step_index, title, detail, image_path, elapsed)``,
        # called after each observation is written. A viewer (the float_steps
        # overlay) uses it to show the frame the model is about to decide on.
        # Best-effort by contract: the loop never lets a viewer raise.
        self.on_step = on_step
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

        history: list[str] = []
        previous = None  # (frame image, history index) of the last executed action
        unchanged = 0
        for step in range(1, max_steps + 1):
            frame = self.capture.capture(self.region)
            if previous is not None:
                before, index, waited = previous
                changed = _screen_changed(before, frame.image)
                history[index] += (
                    " -> screen changed" if changed else " -> screen did NOT change"
                )
                previous = None
                # Waiting is allowed to change nothing; it neither counts
                # toward being stuck nor clears the streak.
                if not waited:
                    unchanged = 0 if changed else unchanged + 1
            if self.max_unchanged and unchanged >= self.max_unchanged:
                reason = (
                    f"stuck: {unchanged} actions in a row did not change the "
                    "screen; history: " + " | ".join(history[-unchanged:])
                )
                emit(run_dir, "run_end", step=step, summary=reason)
                return LoopResult("stuck", step - 1, reason, run_dir)
            gridded = render_numbered_grid(frame.image, self.grid)
            clean_path = run_dir / f"step-{step:03d}-clean.png"
            grid_path = run_dir / f"step-{step:03d}-grid.png"
            frame.image.save(clean_path)
            gridded.save(grid_path)
            emit(
                run_dir,
                "observation",
                step=step,
                summary=f"step {step}/{max_steps} screenshot",
                image=grid_path.name,
            )
            if self.on_step is not None:
                # A viewer failure must never take the run down: the overlay is
                # a display nicety, exactly like the board.
                try:
                    self.on_step(step, f"step {step:03d}",
                                 f"deciding ({len(history)} done)",
                                 str(grid_path), None)
                except Exception:  # noqa: BLE001
                    pass

            action = self.provider.choose_action(
                task=_task_with_history(task, history),
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
                "history": list(history),
                "model_action": action.model_dump(mode="json"),
                "provider_metadata": getattr(self.provider, "last_metadata", None),
                "zoom_trace": zoom_trace,
                "resolved_local_action": (
                    None
                    if resolved is None
                    else {
                        key: value
                        for key, value in asdict(resolved).items()
                        if value not in (None, (), 0, 0.0) or key == "type"
                    }
                ),
            }
            (run_dir / f"step-{step:03d}-action.json").write_text(
                json.dumps(record, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            emit(
                run_dir,
                "action_result",
                step=step,
                summary=_action_summary(action),
                data={
                    "model_action": record["model_action"],
                    "resolved_local_action": record["resolved_local_action"],
                },
            )

            if action.type == "done":
                emit(run_dir, "run_end", step=step, summary=f"done: {action.reason}")
                return LoopResult("done", step, action.reason, run_dir)

            assert resolved is not None
            try:
                self.executor.execute(resolved)
            except ValueError as exc:
                # A bad key name and the like: tell the model next step
                # instead of killing the run. The screen did not change, so
                # this counts toward being stuck.
                history.append(f"{step}. {_action_summary(action)} -> FAILED: {exc}")
                emit(run_dir, "action_error", step=step, summary=str(exc))
                unchanged += 1
            else:
                history.append(f"{step}. {_action_summary(action)}")
                previous = (frame.image, len(history) - 1, action.type == "wait")
            if self.settle_seconds:
                time.sleep(self.settle_seconds)

        emit(run_dir, "run_end", summary=f"stopped after max_steps={max_steps}")
        return LoopResult(
            "max_steps",
            max_steps,
            f"stopped after max_steps={max_steps}",
            run_dir,
        )
