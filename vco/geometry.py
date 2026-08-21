"""Grid numbering and local conversion to logical screen coordinates."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Tuple

from .models import GridPoint, GridSpec, Region


@dataclass(frozen=True)
class CellBounds:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


class GridMapper:
    """Maps cell-relative points to pixels without exposing pixels to the model."""

    def __init__(self, region: Region, grid: GridSpec):
        if region.width < grid.cols or region.height < grid.rows:
            raise ValueError("target region must contain at least one pixel per grid cell")
        self.region = region
        self.grid = grid

    def row_col(self, cell: int) -> Tuple[int, int]:
        if not 1 <= cell <= self.grid.cell_count:
            raise ValueError(
                f"cell {cell} is outside 1..{self.grid.cell_count}"
            )
        index = cell - 1
        return divmod(index, self.grid.cols)

    def cell_bounds(self, cell: int, *, screen: bool = True) -> CellBounds:
        row, col = self.row_col(cell)
        left = math.floor(col * self.region.width / self.grid.cols)
        right = math.floor((col + 1) * self.region.width / self.grid.cols)
        top = math.floor(row * self.region.height / self.grid.rows)
        bottom = math.floor((row + 1) * self.region.height / self.grid.rows)
        if screen:
            left += self.region.x
            right += self.region.x
            top += self.region.y
            bottom += self.region.y
        return CellBounds(left, top, right, bottom)

    def to_screen(self, point: GridPoint) -> Tuple[int, int]:
        bounds = self.cell_bounds(point.cell, screen=True)
        # Offsets address actual pixels: 0 is the first and 1 is the last pixel.
        x = bounds.left + round(point.offset_x * max(0, bounds.width - 1))
        y = bounds.top + round(point.offset_y * max(0, bounds.height - 1))
        return x, y

    def from_screen(self, x: int, y: int) -> GridPoint:
        """Encode a local pixel back into this grid's cell-relative protocol."""

        local_x = x - self.region.x
        local_y = y - self.region.y
        if not (0 <= local_x < self.region.width and 0 <= local_y < self.region.height):
            raise ValueError(f"point ({x}, {y}) is outside the target region")
        # Bounds are built with floor(k * size / parts). The inverse must choose
        # the first boundary strictly greater than the pixel, especially when
        # the region size is not divisible by the grid dimensions.
        col = min(
            self.grid.cols - 1,
            ((local_x + 1) * self.grid.cols - 1) // self.region.width,
        )
        row = min(
            self.grid.rows - 1,
            ((local_y + 1) * self.grid.rows - 1) // self.region.height,
        )
        cell = row * self.grid.cols + col + 1
        bounds = self.cell_bounds(cell, screen=True)
        offset_x = 0.0 if bounds.width == 1 else (x - bounds.left) / (bounds.width - 1)
        offset_y = 0.0 if bounds.height == 1 else (y - bounds.top) / (bounds.height - 1)
        return GridPoint(cell=cell, offset_x=offset_x, offset_y=offset_y)

    def mapping_table(self) -> Dict[str, dict]:
        """Generate a serializable cell-to-screen-bounds lookup table."""

        result = {}
        for cell in range(1, self.grid.cell_count + 1):
            row, col = self.row_col(cell)
            bounds = self.cell_bounds(cell, screen=True)
            result[str(cell)] = {
                "row": row,
                "col": col,
                "screen_bounds": {
                    "left": bounds.left,
                    "top": bounds.top,
                    "right_exclusive": bounds.right,
                    "bottom_exclusive": bounds.bottom,
                },
            }
        return result
