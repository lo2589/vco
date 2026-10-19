"""A floating board that shows the *steps* of a vco run, image by image.

``vco board`` narrates one line of text per action; that is right for a
screencast caption but it throws the screenshot away. This overlay keeps them:
a large image on top (the frame vco actually saw, with its grid overlay and the
hit marker) and a scrollable list of every step underneath, so a viewer can see
what the model looked at at each step and click back through history.

Architecture mirrors ``float_board``: AppKit's run loop needs the process main
thread, so the overlay is a helper subprocess (``python -m vco.float_steps
--port N [--parent-pid P]``) that also embeds a loopback-only HTTP server. The
CLI pushes one JSON record per step; the helper queues it onto the main thread
through ``callAfter`` and redraws.

The retention policy lives in :class:`StepTimeline`, a pure-Python model with
an injectable clock, so the interesting behaviour (append, cap, select, follow
the newest) is unit-testable without a screen.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .net import open_url

# Clear of the board (8931), the panel range (8919-8927), the debug server
# (8767), watch (8766) and ocr-serve (8765).
STEPS_DEFAULT_PORT = 8932

# How many steps the list keeps. The list is scrollable, so this is a memory
# bound, not a display bound; a long run should still be reviewable.
MAX_STEPS = 200

# Window geometry. The image pane keeps the frame's aspect ratio; the list is
# a fixed-height strip so the overlay never grows without bound.
WINDOW_WIDTH = 340
WINDOW_HEIGHT = 480
IMAGE_FRACTION = 0.56
ROW_HEIGHT = 44
THUMB_WIDTH = 56
THUMB_HEIGHT = 34
PADDING = 10

# Screenshots accumulate one run directory per invocation. A directory whose
# artifacts are older than this is fair game for `vco clean`.
DEFAULT_KEEP_DAYS = 7
DEFAULT_KEEP_RUNS = 20

# The overlay must never *flash*: a borderless panel that appears and vanishes
# instantly reads as a glitch, not as a HUD. So it fades in, stays put while
# steps are arriving, and only after a long idle does it fade out and hide.
FADE_IN_SECONDS = 0.35
FADE_OUT_SECONDS = 1.2
IDLE_FADE_SECONDS = 300.0          # 5 minutes of no new step
WINDOW_ALPHA = 0.92


class Step:
    """One step of a run, as the overlay needs it."""

    __slots__ = ("index", "title", "detail", "image", "elapsed", "ts")

    def __init__(self, index: int, title: str, detail: str = "",
                 image: str | None = None, elapsed: float | None = None,
                 ts: float | None = None):
        self.index = int(index)
        self.title = str(title)
        self.detail = str(detail)
        self.image = image
        self.elapsed = elapsed
        self.ts = time.time() if ts is None else float(ts)

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "title": self.title,
            "detail": self.detail,
            "image": self.image,
            "elapsed": self.elapsed,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Step":
        return cls(
            index=data.get("index", 0),
            title=data.get("title", ""),
            detail=data.get("detail", ""),
            image=data.get("image"),
            elapsed=data.get("elapsed"),
            ts=data.get("ts"),
        )


class StepTimeline:
    """The overlay's model: an append-only list of steps plus a cursor.

    Pure Python with an injectable clock so behaviour is testable without a
    screen. Rules:

    * steps are appended in order; the index is assigned here so a caller
      cannot hand out duplicates;
    * at most ``max_steps`` are retained — the oldest fall off the front,
      exactly like ``LineBuffer``'s dwell policy but without the timing;
    * selecting a step *pauses* following the newest one. That is deliberate:
      clicking back through history is an explicit act, and the whole point of
      the list is that a later step must not yank the image away from the one
      being read. :meth:`follow` resumes.
    """

    def __init__(self, max_steps: int = MAX_STEPS, clock=None):
        self.max_steps = int(max_steps)
        self._clock = clock or time.time
        self._steps: list[Step] = []
        self._selected = -1
        self._follow = True

    @property
    def steps(self) -> list[Step]:
        return list(self._steps)

    @property
    def following(self) -> bool:
        return self._follow

    @property
    def selected_index(self) -> int:
        return self._selected

    def push(self, title: str, detail: str = "", image: str | None = None,
             elapsed: float | None = None) -> Step:
        index = (self._steps[-1].index + 1) if self._steps else 1
        step = Step(index, title, detail, image, elapsed, ts=self._clock())
        self._steps.append(step)
        while len(self._steps) > self.max_steps:
            self._steps.pop(0)
        if self._follow:
            self._selected = len(self._steps) - 1
        return step

    def select(self, position: int) -> bool:
        """Select by position in the retained list; pauses following."""
        if not 0 <= position < len(self._steps):
            return False
        self._selected = int(position)
        self._follow = False
        return True

    def follow(self) -> None:
        """Resume following: jump to the newest step and stay there."""
        self._follow = True
        if self._steps:
            self._selected = len(self._steps) - 1

    @property
    def current(self) -> Step | None:
        if not self._steps or self._selected < 0:
            return None
        return self._steps[self._selected]

    def clear(self) -> None:
        self._steps = []
        self._selected = -1
        self._follow = True


# --- client -----------------------------------------------------------------


_APPKIT_PYTHON_CACHE: list[str | None] = []


def _appkit_python() -> str | None:
    """An interpreter that can ``import AppKit``, or None.

    Tries the running interpreter first (the common case: vco is installed in
    a Python that has PyObjC), then a few usual suspects — notably the system
    python, which ships PyObjC on macOS. The result is cached: probing costs a
    subprocess, and the answer cannot change within one process.
    """
    if _APPKIT_PYTHON_CACHE:
        return _APPKIT_PYTHON_CACHE[0]
    candidates = [sys.executable, "/usr/bin/python3",
                  shutil.which("python3") or ""]
    chosen: str | None = None
    for candidate in candidates:
        if not candidate or not os.path.exists(candidate):
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "import AppKit"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            chosen = candidate
            break
    _APPKIT_PYTHON_CACHE.append(chosen)
    return chosen


class StepsClient:
    """Talks to the overlay helper. Best-effort, like BoardClient: the overlay
    is a display nicety and must never take the real command down with it."""

    def __init__(self, port: int = STEPS_DEFAULT_PORT, position: str = "top",
                 watchdog: bool = True, screen: int | None = None,
                 width: int = WINDOW_WIDTH, height: int = WINDOW_HEIGHT,
                 idle: float = IDLE_FADE_SECONDS):
        self.port = int(port)
        self.position = position
        self.watchdog = watchdog
        self.screen = screen
        self.width = int(width)
        self.height = int(height)
        self.idle = float(idle)
        self._supported = sys.platform == "darwin"

    def _request(self, method: str, path: str, payload: dict | None = None,
                 timeout: float = 1.5) -> dict | None:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with open_url(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def healthy(self) -> bool:
        if not self._supported:
            return False
        answer = self._request("GET", "/health", timeout=1.0)
        return bool(answer and answer.get("ok"))

    def ensure_running(self, timeout: float = 6.0) -> bool:
        if not self._supported:
            return False
        if self.healthy():
            return True
        # The *client* never needs AppKit — only the helper subprocess does, so
        # pick an interpreter that can actually draw. This matters when vco runs
        # under a Python without PyObjC (a venv, Homebrew's python@3.11) while
        # the system python has it: spawning sys.executable would only spawn a
        # helper that dies on ``import AppKit``.
        python = _appkit_python()
        if python is None:
            return False
        argv = [
            python, "-m", "vco.float_steps",
            "--port", str(self.port),
            "--position", self.position,
            "--width", str(self.width),
            "--height", str(self.height),
            "--idle", str(self.idle),
        ]
        if self.watchdog:
            argv += ["--parent-pid", str(os.getpid())]
        if self.screen is not None:
            argv += ["--screen", str(self.screen)]
        try:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError:
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.healthy():
                return True
            time.sleep(0.15)
        return False

    def push(self, step: dict) -> bool:
        answer = self._request("POST", "/step", {"step": step})
        return bool(answer and answer.get("ok"))

    def clear(self) -> bool:
        answer = self._request("POST", "/clear")
        return bool(answer and answer.get("ok"))

    def show(self) -> bool:
        answer = self._request("POST", "/show")
        return bool(answer and answer.get("ok"))

    def hide(self) -> bool:
        answer = self._request("POST", "/hide")
        return bool(answer and answer.get("ok"))

    def stop(self) -> bool:
        answer = self._request("POST", "/stop")
        return bool(answer and answer.get("ok"))


# --- artifact cleaning -------------------------------------------------------


def _run_dirs(root: Path) -> list[Path]:
    """Run directories under an artifact root, newest first.

    A run directory is any directory directly under ``root`` that holds step
    images. Loose images (``shot-*.png``, ``find-*.marked.png``) live directly
    in ``root`` and are handled by the caller.
    """
    if not root.is_dir():
        return []
    found: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if any(child.glob("step-*")) or any(child.glob("*.png")):
            found.append(child)
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def clean_artifacts(root: Path, *, keep_days: float | None = DEFAULT_KEEP_DAYS,
                    keep_runs: int | None = DEFAULT_KEEP_RUNS,
                    dry_run: bool = True) -> dict:
    """Delete old run directories and loose images under an artifact root.

    Two independent retention rules, both optional:

    * ``keep_runs`` — the newest N run directories survive regardless of age;
    * ``keep_days`` — anything older than this many days goes, even a run
      directory inside the newest N.

    ``dry_run=True`` (the default) reports what *would* be removed and touches
    nothing. Returns a summary dict with the freed bytes and the paths, so the
    CLI can print it and a test can assert on it.
    """
    root = Path(root)
    removed: list[str] = []
    kept: list[str] = []
    freed = 0

    dirs = _run_dirs(root)
    now = time.time()
    for position, run in enumerate(dirs):
        age_days = (now - run.stat().st_mtime) / 86400.0
        too_old = keep_days is not None and age_days > keep_days
        beyond_cap = keep_runs is not None and position >= keep_runs
        if too_old or beyond_cap:
            size = _tree_size(run)
            removed.append(str(run))
            freed += size
            if not dry_run:
                shutil.rmtree(run, ignore_errors=True)
        else:
            kept.append(str(run))

    # Loose images directly under root: same age rule, no run cap.
    if keep_days is not None and root.is_dir():
        for image in sorted(root.glob("*.png")):
            age_days = (now - image.stat().st_mtime) / 86400.0
            if age_days <= keep_days:
                continue
            freed += image.stat().st_size
            removed.append(str(image))
            if not dry_run:
                image.unlink(missing_ok=True)

    return {
        "root": str(root),
        "dry_run": bool(dry_run),
        "removed": removed,
        "kept": kept,
        "removed_count": len(removed),
        "freed_bytes": freed,
    }


def _tree_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


# --- helper subprocess (the AppKit side) -------------------------------------


def _helper_main(argv: list[str] | None = None) -> int:
    import argparse
    import queue
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    parser = argparse.ArgumentParser(prog="python -m vco.float_steps")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--parent-pid", type=int, default=None)
    parser.add_argument("--position", choices=("top", "bottom"), default="top")
    parser.add_argument("--screen", type=int, default=None)
    parser.add_argument("--width", type=int, default=WINDOW_WIDTH)
    parser.add_argument("--height", type=int, default=WINDOW_HEIGHT)
    parser.add_argument(
        "--idle",
        type=float,
        default=IDLE_FADE_SECONDS,
        help="seconds of no new step before the overlay fades out and hides "
             "(0 disables the idle fade; the panel still fades in on demand)",
    )
    args = parser.parse_args(argv)
    idle_seconds = max(0.0, args.idle)

    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSBackingStoreBuffered,
        NSBezierPath,
        NSColor,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSImage,
        NSImageView,
        NSImageScaleProportionallyUpOrDown,
        NSMakePoint,
        NSMakeRect,
        NSPanel,
        NSScreen,
        NSScrollView,
        NSStatusWindowLevel,
        NSTableColumn,
        NSTableView,
        NSTextField,
        NSTimer,
        NSView,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorStationary,
        NSWindowStyleMaskBorderless,
        NSWindowStyleMaskNonactivatingPanel,
    )
    from Foundation import NSString
    from PyObjCTools.AppHelper import callAfter, callLater

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    font = NSFont.boldSystemFontOfSize_(13)
    small = NSFont.systemFontOfSize_(11)

    ops: "queue.Queue[tuple[str, object]]" = queue.Queue()
    timeline = StepTimeline()

    screens = NSScreen.screens()
    if args.screen is not None and 0 <= args.screen < len(screens):
        screen_frame = screens[args.screen].visibleFrame()
    else:
        screen_frame = NSScreen.mainScreen().visibleFrame()

    width = args.width
    height = args.height

    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, width, height),
        NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered,
        False,
    )
    panel.setLevel_(NSStatusWindowLevel)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setIgnoresMouseEvents_(False)
    panel.setHasShadow_(True)
    panel.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
    )

    content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))

    image_h = int(height * IMAGE_FRACTION)
    image_view = NSImageView.alloc().initWithFrame_(
        NSMakeRect(0, height - image_h, width, image_h))
    image_view.setImageScaling_(NSImageScaleProportionallyUpOrDown)
    content.addSubview_(image_view)

    table_h = height - image_h
    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, table_h))
    scroll.setHasVerticalScroller_(True)
    scroll.setDrawsBackground_(False)
    table = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, width, table_h))
    column = NSTableColumn.alloc().initWithIdentifier_("step")
    column.setWidth_(width)
    table.addTableColumn_(column)
    scroll.setDocumentView_(table)
    content.addSubview_(scroll)

    panel.setContentView_(content)

    def _position_window() -> None:
        x = screen_frame.origin.x + screen_frame.size.width - width - 40
        if args.position == "top":
            y = screen_frame.origin.y + screen_frame.size.height - height - 40
        else:
            y = screen_frame.origin.y + 40
        panel.setFrame_display_(NSMakeRect(x, y, width, height), True)

    # --- fade in / idle fade out -------------------------------------------
    #
    # State machine, all on the main thread:
    #
    #   hidden --> (step) fade in --> visible --(5 min idle)--> fade out --> hidden
    #
    # Any step during a fade-out cancels it and fades back in. `_fade_state`
    # is ("idle"|"in"|"out", started_at) and `_last_activity` is reset by every
    # applied op, so `--idle` is measured from the last thing that happened.
    _fade_state = ["idle", 0.0]
    _last_activity = [time.monotonic()]

    def _tick_fade(_timer) -> None:
        now = time.monotonic()
        state, started = _fade_state
        if state == "in":
            progress = (now - started) / FADE_IN_SECONDS
            if progress >= 1.0:
                panel.setAlphaValue_(WINDOW_ALPHA)
                _fade_state[0] = "idle"
            else:
                panel.setAlphaValue_(WINDOW_ALPHA * progress)
        elif state == "out":
            progress = (now - started) / FADE_OUT_SECONDS
            if progress >= 1.0:
                panel.setAlphaValue_(0.0)
                panel.orderOut_(None)
                _fade_state[0] = "idle"
            else:
                panel.setAlphaValue_(WINDOW_ALPHA * (1.0 - progress))
        elif idle_seconds and now - _last_activity[0] >= idle_seconds:
            # Nothing has happened for a while: begin a gentle fade, not a cut.
            _fade_state[0] = "out"
            _fade_state[1] = now

    def _fade_in() -> None:
        # If the panel is visible and fully opaque there is nothing to do —
        # re-fading on every step would itself read as flicker.
        if panel.isVisible() and panel.alphaValue() >= WINDOW_ALPHA - 0.01:
            _fade_state[0] = "idle"
            return
        if _fade_state[0] != "in":
            _fade_state[0] = "in"
            _fade_state[1] = time.monotonic()
        panel.setAlphaValue_(0.0)
        panel.orderFrontRegardless()

    def _apply(op: str, payload: object) -> None:
        _last_activity[0] = time.monotonic()
        if op == "step":
            step = Step.from_dict(payload or {})
            timeline.push(step.title, step.detail, step.image, step.elapsed)
            table.reloadData()
            table.scrollRowToVisible_(len(timeline.steps) - 1)
            current = timeline.current
            if current and current.image:
                image = NSImage.alloc().initWithContentsOfFile_(current.image)
                if image is not None:
                    image_view.setImage_(image)
            _fade_in()
        elif op == "clear":
            timeline.clear()
            table.reloadData()
            image_view.setImage_(None)
        elif op == "show":
            _last_activity[0] = time.monotonic()
            _fade_in()
        elif op == "hide":
            _fade_state[0] = "out"
            _fade_state[1] = time.monotonic()
        elif op == "stop":
            callLater(0.25, lambda: app.terminate_(None))

    def _drain() -> None:
        while True:
            try:
                op, payload = ops.get_nowait()
            except queue.Empty:
                break
            _apply(op, payload)

    def _enqueue(op: str, payload: object = None) -> None:
        ops.put((op, payload))
        callAfter(_drain)

    class StepHandler(BaseHTTPRequestHandler):
        def _json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                self._json({
                    "ok": True,
                    "steps": [s.as_dict() for s in timeline.steps],
                    "selected": timeline.selected_index,
                    "following": timeline.following,
                })
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):  # noqa: N802
            if self.path == "/step":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}")
                    step = body.get("step") or {}
                except Exception:  # noqa: BLE001
                    self._json({"error": "bad request"}, 400)
                    return
                _enqueue("step", step)
                self._json({"ok": True})
            elif self.path in ("/clear", "/show", "/hide", "/stop"):
                _enqueue(self.path.lstrip("/"))
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", args.port), StepHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def _watchdog(_timer) -> None:
        try:
            os.kill(args.parent_pid, 0)
        except OSError:
            app.terminate_(None)

    if args.parent_pid is not None:
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.5, True, _watchdog)

    # 20 Hz is smooth enough for a 0.35 s fade and cheap for a HUD. The panel
    # starts hidden; the first step fades it in, so `vco float` never flashes
    # an empty window on screen.
    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.05, True, _tick_fade)

    panel.setAlphaValue_(0.0)
    _position_window()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(_helper_main())
