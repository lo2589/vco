"""Validated data exchanged between the model and the local controller."""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Region(StrictModel):
    """Target region in logical screen coordinates."""

    x: int = 0
    y: int = 0
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class GridSpec(StrictModel):
    rows: int = Field(default=10, ge=1, le=50)
    cols: int = Field(default=10, ge=1, le=50)

    @property
    def cell_count(self) -> int:
        return self.rows * self.cols


class GridPoint(StrictModel):
    """A point expressed without raw screen pixels."""

    cell: int = Field(ge=1, description="One-based, row-major cell number")
    offset_x: float = Field(ge=0.0, le=1.0)
    offset_y: float = Field(ge=0.0, le=1.0)


class CenterDeltaTarget(StrictModel):
    """Fine-grid target measured from the centered number badge in image pixels."""

    cell: int = Field(ge=1)
    delta_x_px: int
    delta_y_px: int


class CenterDeltaClick(StrictModel):
    type: Literal["click"]
    target: CenterDeltaTarget


class ClickAction(StrictModel):
    type: Literal["click"]
    target: GridPoint


class DragAction(StrictModel):
    type: Literal["drag"]
    start: GridPoint
    end: GridPoint


class DoubleClickAction(StrictModel):
    type: Literal["double_click"]
    target: GridPoint


class RightClickAction(StrictModel):
    type: Literal["right_click"]
    target: GridPoint


class ScrollAction(StrictModel):
    """Scroll with the pointer over ``target``; positive ``amount`` scrolls down."""

    type: Literal["scroll"]
    target: GridPoint
    amount: int = Field(ge=-30, le=30)


class TypeTextAction(StrictModel):
    """Type into whatever currently has keyboard focus."""

    type: Literal["type"]
    text: str = Field(min_length=1, max_length=2000)


MODIFIER_KEYS = {"command", "ctrl", "option", "alt", "shift", "fn", "win"}
_KEY_ALIASES = {
    "cmd": "command",
    "control": "ctrl",
    "opt": "option",
    "return": "enter",
    "esc": "escape",
    "del": "delete",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
}


def normalize_keys(combo: str) -> tuple[str, ...]:
    """Split ``"cmd+shift+t"`` into pyautogui key names, rejecting junk.

    Every part but the last must be a modifier, so "a+b" (two plain keys) is
    refused instead of being pressed as a chord nobody meant.
    """

    parts = [part.strip().lower() for part in combo.split("+")]
    if not parts or any(not part for part in parts):
        raise ValueError(f"malformed key combo: {combo!r}")
    parts = [_KEY_ALIASES.get(part, part) for part in parts]
    for part in parts[:-1]:
        if part not in MODIFIER_KEYS:
            raise ValueError(f"{part!r} is not a modifier in {combo!r}")
    if len(set(parts)) != len(parts):
        raise ValueError(f"repeated key in {combo!r}")
    return tuple(parts)


class KeyAction(StrictModel):
    """Press one key or a chord, e.g. ``enter``, ``cmd+space``, ``cmd+shift+t``."""

    type: Literal["key"]
    keys: str = Field(min_length=1, max_length=60)

    @field_validator("keys")
    @classmethod
    def _valid_combo(cls, value: str) -> str:
        return "+".join(normalize_keys(value))


class WaitAction(StrictModel):
    type: Literal["wait"]
    seconds: float = Field(ge=0.1, le=10.0)


class DoneAction(StrictModel):
    type: Literal["done"]
    reason: str = Field(min_length=1, max_length=500)


Action = Annotated[
    Union[
        ClickAction,
        DoubleClickAction,
        RightClickAction,
        DragAction,
        ScrollAction,
        TypeTextAction,
        KeyAction,
        WaitAction,
        DoneAction,
    ],
    Field(discriminator="type"),
]
ACTION_ADAPTER = TypeAdapter(Action)


def parse_action(value: object) -> Action:
    """Parse either a JSON string/bytes value or a decoded JSON object."""

    if isinstance(value, (str, bytes, bytearray)):
        return ACTION_ADAPTER.validate_json(value)
    return ACTION_ADAPTER.validate_python(value)


def action_json_schema() -> dict:
    """Return the same strict schema used for local validation."""

    return ACTION_ADAPTER.json_schema()


def structured_action_json_schema() -> dict:
    """Return a root-object schema suitable for strict structured outputs.

    The local protocol is a discriminated union. Some structured-output APIs
    require the root itself to be an object, so the transport wraps it in an
    ``action`` property while the rest of the application keeps the lean shape.
    """

    inner = action_json_schema()
    definitions = inner.pop("$defs", {})
    inner.pop("discriminator", None)
    if "oneOf" in inner:
        inner["anyOf"] = inner.pop("oneOf")
    return {
        "$defs": definitions,
        "type": "object",
        "properties": {"action": inner},
        "required": ["action"],
        "additionalProperties": False,
    }


def structured_click_json_schema() -> dict:
    """Return a strict transport schema that permits only click localization."""

    inner = ClickAction.model_json_schema()
    definitions = inner.pop("$defs", {})
    return {
        "$defs": definitions,
        "type": "object",
        "properties": {"action": inner},
        "required": ["action"],
        "additionalProperties": False,
    }


def structured_center_delta_schema(
    *, cell_count: int, max_delta_x: int, max_delta_y: int
) -> dict:
    """Build a strict click schema using signed pixels from the number center."""

    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "const": "click"},
                    "target": {
                        "type": "object",
                        "properties": {
                            "cell": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": cell_count,
                            },
                            "delta_x_px": {
                                "type": "integer",
                                "minimum": -max_delta_x,
                                "maximum": max_delta_x,
                            },
                            "delta_y_px": {
                                "type": "integer",
                                "minimum": -max_delta_y,
                                "maximum": max_delta_y,
                            },
                        },
                        "required": ["cell", "delta_x_px", "delta_y_px"],
                        "additionalProperties": False,
                    },
                },
                "required": ["type", "target"],
                "additionalProperties": False,
            }
        },
        "required": ["action"],
        "additionalProperties": False,
    }


def parse_center_delta(value: object) -> CenterDeltaClick:
    if isinstance(value, (str, bytes, bytearray)):
        return CenterDeltaClick.model_validate_json(value)
    return CenterDeltaClick.model_validate(value)


def points_in_action(action: Action):
    if action.type in {"click", "double_click", "right_click", "scroll"}:
        return (action.target,)
    if action.type == "drag":
        return (action.start, action.end)
    return ()
