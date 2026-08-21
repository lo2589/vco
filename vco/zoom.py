"""Two-stage coarse-to-fine grid selection for small vision models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from PIL import Image

from .geometry import GridMapper
from .grid import render_numbered_grid
from .models import Action, GridPoint, GridSpec, Region, parse_action


@dataclass(frozen=True)
class ZoomTrace:
    coarse_action: dict
    crop_box: Tuple[int, int, int, int] | None
    fine_action: dict | None
    fine_center_delta: dict | None
    final_action: dict

    def as_dict(self) -> dict:
        return {
            "coarse_action": self.coarse_action,
            "crop_box_image_pixels": self.crop_box,
            "fine_action": self.fine_action,
            "fine_center_delta": self.fine_center_delta,
            "final_action": self.final_action,
        }


def neighborhood_crop_box(
    image_size: Tuple[int, int],
    grid: GridSpec,
    cell: int,
    *,
    span_cells: float = 2.0,
) -> Tuple[int, int, int, int]:
    """Return a fixed-size crop centered on a selected coarse cell.

    At image edges the window shifts inward instead of shrinking, preserving a
    consistent fine-grid scale.
    """

    width, height = image_size
    mapper = GridMapper(Region(x=0, y=0, width=width, height=height), grid)
    bounds = mapper.cell_bounds(cell, screen=False)
    crop_width = min(width, max(1, round(width / grid.cols * span_cells)))
    crop_height = min(height, max(1, round(height / grid.rows * span_cells)))
    center_x = (bounds.left + bounds.right) / 2
    center_y = (bounds.top + bounds.bottom) / 2
    left = round(center_x - crop_width / 2)
    top = round(center_y - crop_height / 2)
    left = min(max(0, left), width - crop_width)
    top = min(max(0, top), height - crop_height)
    return left, top, left + crop_width, top + crop_height


class ZoomProvider:
    """Refine a coarse grid choice inside a configurable local fine grid."""

    def __init__(
        self,
        provider,
        *,
        fine_grid: GridSpec | None = None,
        span_cells: float = 2.0,
        use_model_offset: bool = False,
        use_center_delta: bool = False,
        force_initial_click: bool = True,
        resize_crop_to_input: bool = False,
    ):
        self.provider = provider
        self.fine_grid = fine_grid or GridSpec(rows=4, cols=4)
        self.span_cells = span_cells
        self.use_model_offset = use_model_offset
        self.use_center_delta = use_center_delta
        self.force_initial_click = force_initial_click
        self.resize_crop_to_input = resize_crop_to_input
        self.last_trace: ZoomTrace | None = None
        self.last_zoom_clean: Image.Image | None = None
        self.last_zoom_grid: Image.Image | None = None

    def choose_action(
        self,
        *,
        task: str,
        clean: Image.Image,
        gridded: Image.Image,
        grid: GridSpec,
        step: int,
    ) -> Action:
        coarse = self.provider.choose_action(
            task=task,
            clean=clean,
            gridded=gridded,
            grid=grid,
            step=step,
            force_click=self.force_initial_click,
        )
        if coarse.type != "click":
            dumped = coarse.model_dump(mode="json")
            self.last_trace = ZoomTrace(dumped, None, None, None, dumped)
            self.last_zoom_clean = None
            self.last_zoom_grid = None
            return coarse

        crop_box = neighborhood_crop_box(
            clean.size,
            grid,
            coarse.target.cell,
            span_cells=self.span_cells,
        )
        zoom_clean = clean.crop(crop_box)
        if self.resize_crop_to_input:
            zoom_clean = zoom_clean.resize(clean.size, Image.Resampling.LANCZOS)
        zoom_grid = render_numbered_grid(
            zoom_clean,
            self.fine_grid,
            label_position="center",
        )
        fine = self.provider.choose_action(
            task=(
                f"{task}\n"
                f"Zoom stage: this image is the {self.span_cells:g}x{self.span_cells:g} "
                f"coarse-cell neighborhood around previously selected cell "
                f"{coarse.target.cell}. Select the target again in this new "
                f"{self.fine_grid.rows}x{self.fine_grid.cols} grid."
            ),
            clean=zoom_clean,
            gridded=zoom_grid,
            grid=self.fine_grid,
            step=step,
            force_click=True,
            center_delta=self.use_center_delta,
        )
        fine_center_delta = getattr(self.provider, "last_center_delta", None)
        self.last_zoom_clean = zoom_clean
        self.last_zoom_grid = zoom_grid
        if fine.type != "click":
            dumped = fine.model_dump(mode="json")
            self.last_trace = ZoomTrace(
                coarse.model_dump(mode="json"),
                crop_box,
                dumped,
                fine_center_delta,
                dumped,
            )
            return fine

        selected = (
            fine.target
            if self.use_model_offset
            or (self.use_center_delta and fine_center_delta is not None)
            else GridPoint(cell=fine.target.cell, offset_x=0.5, offset_y=0.5)
        )
        zoom_mapper = GridMapper(
            Region(x=0, y=0, width=zoom_clean.width, height=zoom_clean.height),
            self.fine_grid,
        )
        zoom_x, zoom_y = zoom_mapper.to_screen(selected)
        if self.resize_crop_to_input:
            crop_width = crop_box[2] - crop_box[0]
            crop_height = crop_box[3] - crop_box[1]
            full_x = crop_box[0] + round(
                zoom_x * max(0, crop_width - 1) / max(1, zoom_clean.width - 1)
            )
            full_y = crop_box[1] + round(
                zoom_y * max(0, crop_height - 1) / max(1, zoom_clean.height - 1)
            )
        else:
            full_x = crop_box[0] + zoom_x
            full_y = crop_box[1] + zoom_y
        full_mapper = GridMapper(
            Region(x=0, y=0, width=clean.width, height=clean.height), grid
        )
        final_point = full_mapper.from_screen(full_x, full_y)
        final = parse_action(
            {"type": "click", "target": final_point.model_dump(mode="json")}
        )
        self.last_trace = ZoomTrace(
            coarse.model_dump(mode="json"),
            crop_box,
            fine.model_dump(mode="json"),
            fine_center_delta,
            final.model_dump(mode="json"),
        )
        return final
