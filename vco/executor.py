"""Safe action execution backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Tuple

from .models import Action


Point = Tuple[int, int]


@dataclass(frozen=True)
class ResolvedAction:
    type: str
    start: Point
    end: Point | None = None
    duration: float = 0.0
    button: str = "left"


class ActionExecutor(Protocol):
    def execute(self, action: ResolvedAction) -> None: ...


class DryRunExecutor:
    """Record the latest action without touching the user's mouse."""

    def __init__(self):
        self.actions = []

    def execute(self, action: ResolvedAction) -> None:
        self.actions.append(action)
        print(f"DRY RUN: {action}")


class PyAutoGuiExecutor:
    """Real mouse control. Import is delayed so core features stay lightweight."""

    def __init__(self, pause: float = 0.1):
        try:
            import pyautogui
        except ImportError as exc:
            raise RuntimeError(
                "real mouse control requires: pip install -e '.[control]'"
            ) from exc
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = pause
        self._driver = pyautogui

    def execute(self, action: ResolvedAction) -> None:
        if action.type == "click":
            self._driver.click(*action.start, button=action.button)
            return
        if action.type == "drag":
            assert action.end is not None
            self._driver.moveTo(*action.start)
            self._driver.dragTo(
                *action.end,
                duration=action.duration,
                button=action.button,
            )
            return
        raise ValueError(f"unsupported resolved action: {action.type}")


def resolve_action(action: Action, mapper) -> ResolvedAction | None:
    if action.type == "done":
        return None
    if action.type == "click":
        return ResolvedAction(
            type="click",
            start=mapper.to_screen(action.target),
        )
    return ResolvedAction(
        type="drag",
        start=mapper.to_screen(action.start),
        end=mapper.to_screen(action.end),
        duration=0.4,
    )
