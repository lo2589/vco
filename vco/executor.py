"""Safe action execution backends."""

from __future__ import annotations

import atexit
import sys
import time
from dataclasses import dataclass
from typing import Protocol, Tuple

from .cursor_badge import AgentCursorBadge
from .models import Action, normalize_keys


Point = Tuple[int, int]


@dataclass(frozen=True)
class ResolvedAction:
    type: str
    start: Point | None = None
    end: Point | None = None
    duration: float = 0.0
    button: str = "left"
    clicks: int = 1
    amount: int = 0
    text: str | None = None
    keys: tuple[str, ...] = ()
    seconds: float = 0.0


class ActionExecutor(Protocol):
    def execute(self, action: ResolvedAction) -> None: ...


class DryRunExecutor:
    """Record the latest action without touching the user's mouse."""

    def __init__(self):
        self.actions = []

    def execute(self, action: ResolvedAction) -> None:
        self.actions.append(action)
        print(f"DRY RUN: {action}")


def input_text(text: str, *, paste: bool = False, press_enter: int = 0) -> None:
    """Type or paste ``text`` at the current keyboard focus."""

    import pyautogui

    if paste:
        import pyperclip

        pyperclip.copy(text)
        modifier = "command" if sys.platform == "darwin" else "ctrl"
        pyautogui.keyDown(modifier)
        pyautogui.keyDown("v")
        pyautogui.keyUp("v")
        pyautogui.keyUp(modifier)
    else:
        pyautogui.typewrite(text, interval=0.01)
    for _ in range(press_enter):
        pyautogui.press("return")


class PyAutoGuiExecutor:
    """Real mouse control. Import is delayed so core features stay lightweight."""

    def __init__(self, pause: float = 0.1, cursor_badge: bool = True):
        try:
            import pyautogui
        except ImportError as exc:
            raise RuntimeError(
                "real mouse control requires: pip install -e '.[control]'"
            ) from exc
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = pause
        self._driver = pyautogui
        self._badge = AgentCursorBadge(enabled=cursor_badge)
        self._badge_started = False

    def _ensure_badge(self) -> None:
        # Lazy: the badge appears with the FIRST real action, not at executor
        # construction — a process that never moves the mouse never claims to
        # be driving it. atexit (not __del__) guarantees the helper is reaped
        # even when the command path returns early.
        if self._badge_started:
            return
        self._badge_started = True
        self._badge.start()
        atexit.register(self._badge.stop)

    def execute(self, action: ResolvedAction) -> None:
        self._ensure_badge()
        if action.type == "click":
            self._driver.click(
                *action.start, clicks=action.clicks, button=action.button
            )
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
        if action.type == "scroll":
            self._driver.moveTo(*action.start)
            # pyautogui scrolls up for positive values; the protocol says
            # positive is down, matching how people describe reading on.
            self._driver.scroll(-action.amount)
            return
        if action.type == "type":
            assert action.text is not None
            # typewrite only knows ASCII; anything else goes through the
            # clipboard so CJK text and emoji arrive intact.
            input_text(action.text, paste=not action.text.isascii())
            return
        if action.type == "key":
            unknown = [
                key for key in action.keys if key not in self._driver.KEYBOARD_KEYS
            ]
            if unknown:
                raise ValueError(f"unknown key(s): {', '.join(unknown)}")
            self._driver.hotkey(*action.keys)
            return
        if action.type == "wait":
            time.sleep(action.seconds)
            return
        raise ValueError(f"unsupported resolved action: {action.type}")


def resolve_action(action: Action, mapper) -> ResolvedAction | None:
    if action.type == "done":
        return None
    if action.type == "click":
        return ResolvedAction(type="click", start=mapper.to_screen(action.target))
    if action.type == "double_click":
        return ResolvedAction(
            type="click", start=mapper.to_screen(action.target), clicks=2
        )
    if action.type == "right_click":
        return ResolvedAction(
            type="click", start=mapper.to_screen(action.target), button="right"
        )
    if action.type == "scroll":
        return ResolvedAction(
            type="scroll",
            start=mapper.to_screen(action.target),
            amount=action.amount,
        )
    if action.type == "type":
        return ResolvedAction(type="type", text=action.text)
    if action.type == "key":
        return ResolvedAction(type="key", keys=normalize_keys(action.keys))
    if action.type == "wait":
        return ResolvedAction(type="wait", seconds=action.seconds)
    return ResolvedAction(
        type="drag",
        start=mapper.to_screen(action.start),
        end=mapper.to_screen(action.end),
        duration=0.4,
    )
