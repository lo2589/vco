"""Model-directed recursive zoom for dense-grid visual localization."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Tuple

from PIL import Image

from .geometry import GridMapper
from .grid import render_numbered_grid
from .models import Action, GridSpec, Region, parse_action
from .zoom import neighborhood_crop_box


Box = Tuple[int, int, int, int]


@dataclass(frozen=True)
class AdaptiveZoomLevel:
    level: int
    view_box: Box
    decision: dict
    next_view_box: Box | None
    provider_metadata: dict | None

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "view_box_image_pixels": self.view_box,
            "decision": self.decision,
            "next_view_box_image_pixels": self.next_view_box,
            "provider_metadata": self.provider_metadata,
        }


@dataclass(frozen=True)
class AdaptiveZoomTrace:
    levels: tuple[AdaptiveZoomLevel, ...]
    final_action: dict

    def as_dict(self) -> dict:
        return {
            "mode": "model-directed-adaptive-zoom",
            "levels": [level.as_dict() for level in self.levels],
            "final_action": self.final_action,
        }


def _map_crop_to_original(display_crop: Box, view_box: Box, display_size) -> Box:
    display_width, display_height = display_size
    view_left, view_top, view_right, view_bottom = view_box
    view_width = view_right - view_left
    view_height = view_bottom - view_top
    left, top, right, bottom = display_crop
    mapped = (
        view_left + math.floor(left * view_width / display_width),
        view_top + math.floor(top * view_height / display_height),
        view_left + math.ceil(right * view_width / display_width),
        view_top + math.ceil(bottom * view_height / display_height),
    )
    if mapped[2] <= mapped[0] or mapped[3] <= mapped[1]:
        raise ValueError("adaptive zoom produced an empty crop")
    return mapped


def _map_point_to_original(point, view_box: Box, display_size):
    x, y = point
    display_width, display_height = display_size
    left, top, right, bottom = view_box
    view_width = right - left
    view_height = bottom - top
    original_x = left + round(x * max(0, view_width - 1) / max(1, display_width - 1))
    original_y = top + round(y * max(0, view_height - 1) / max(1, display_height - 1))
    return original_x, original_y


class AdaptiveZoomProvider:
    """Ask the model to click now or request another zoom level."""

    def __init__(
        self,
        provider,
        *,
        max_levels: int = 3,
        span_cells: float = 4.0,
        force_initial_click: bool = False,
        center_click_zoom_epsilon: float | None = 0.02,
        always_refine: bool = False,
    ):
        if max_levels < 1:
            raise ValueError("max_levels must be at least 1")
        if span_cells <= 1:
            raise ValueError("span_cells must be greater than 1")
        if center_click_zoom_epsilon is not None and not (
            0 <= center_click_zoom_epsilon < 0.5
        ):
            raise ValueError("center_click_zoom_epsilon must be in [0,0.5)")
        self.provider = provider
        self.max_levels = max_levels
        self.span_cells = span_cells
        self.force_initial_click = force_initial_click
        self.center_click_zoom_epsilon = center_click_zoom_epsilon
        self.always_refine = always_refine
        self.last_trace: AdaptiveZoomTrace | None = None
        self.last_zoom_images: list[tuple[Image.Image, Image.Image]] = []
        self.last_metadata = None

    def choose_action(
        self,
        *,
        task: str,
        clean: Image.Image,
        gridded: Image.Image,
        grid: GridSpec,
        step: int,
    ) -> Action:
        original = clean
        full_box = (0, 0, clean.width, clean.height)
        view_box = full_box
        view_clean = clean
        view_grid = gridded
        levels = []
        call_metadata = []
        self.last_zoom_images = []

        for level in range(1, self.max_levels + 1):
            level_task = task
            if level > 1:
                level_task += (
                    f"\nAdaptive zoom level {level}: this is an enlarged crop from "
                    "the previous requested zoom. Re-evaluate whether the target can "
                    "now be clicked reliably."
                )
            chooser = (
                self.provider.choose_action
                if self.always_refine or level == self.max_levels
                else self.provider.choose_action_or_zoom
            )
            decision = chooser(
                task=level_task,
                clean=view_clean,
                gridded=view_grid,
                grid=grid,
                step=step,
                force_click=self.force_initial_click or level > 1,
            )
            metadata = getattr(self.provider, "last_metadata", None)
            call_metadata.append(metadata)

            if (
                not isinstance(decision, dict)
                and decision.type == "click"
                and level < self.max_levels
                and not (metadata or {}).get("ocr_direct", False)
                and (
                    self.always_refine
                    or (
                        self.center_click_zoom_epsilon is not None
                        and abs(decision.target.offset_x - 0.5)
                        <= self.center_click_zoom_epsilon
                        and abs(decision.target.offset_y - 0.5)
                        <= self.center_click_zoom_epsilon
                    )
                )
            ):
                decision = {
                    "type": "zoom",
                    "cell": decision.target.cell,
                    "reason": (
                        "fixed-refinement"
                        if self.always_refine
                        else "model-returned-suspicious-cell-center"
                    ),
                }

            if isinstance(decision, dict):
                display_crop = neighborhood_crop_box(
                    view_clean.size,
                    grid,
                    decision["cell"],
                    span_cells=self.span_cells,
                )
                next_view_box = _map_crop_to_original(
                    display_crop, view_box, view_clean.size
                )
                levels.append(
                    AdaptiveZoomLevel(
                        level,
                        view_box,
                        dict(decision),
                        next_view_box,
                        metadata,
                    )
                )
                view_box = next_view_box
                view_clean = original.crop(view_box).resize(
                    original.size, Image.Resampling.LANCZOS
                )
                view_grid = render_numbered_grid(view_clean, grid)
                self.last_zoom_images.append((view_clean, view_grid))
                continue

            dumped = decision.model_dump(mode="json")
            if decision.type == "done":
                levels.append(
                    AdaptiveZoomLevel(level, view_box, dumped, None, metadata)
                )
                final = decision
            elif decision.type == "click":
                local_mapper = GridMapper(
                    Region(width=view_clean.width, height=view_clean.height), grid
                )
                local_point = local_mapper.to_screen(decision.target)
                full_x, full_y = _map_point_to_original(
                    local_point, view_box, view_clean.size
                )
                full_mapper = GridMapper(
                    Region(width=original.width, height=original.height), grid
                )
                final_point = full_mapper.from_screen(full_x, full_y)
                final = parse_action(
                    {"type": "click", "target": final_point.model_dump(mode="json")}
                )
                levels.append(
                    AdaptiveZoomLevel(level, view_box, dumped, None, metadata)
                )
            else:
                raise RuntimeError("adaptive zoom currently supports click or done")

            self.last_trace = AdaptiveZoomTrace(
                tuple(levels), final.model_dump(mode="json")
            )
            self.last_metadata = {
                "adaptive_zoom_levels": level,
                "calls": call_metadata,
                "total_elapsed_seconds": round(
                    sum((item or {}).get("elapsed_seconds", 0) for item in call_metadata),
                    3,
                ),
            }
            return final

        raise AssertionError("adaptive zoom loop ended without a decision")
