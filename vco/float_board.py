"""A translucent floating board that narrates what the agent is doing.

When someone records a vco session, the video shows clicks and keystrokes but
not *why* anything happens. The board is a click-through overlay pinned to the
screen edge: vco (or any script driving it) POSTs short lines like
"点击 保存按钮" / "key cmd+v", and the recording's audience can follow each
step as it happens.

Architecture mirrors cursor_badge, for the same reason: AppKit's run loop
must live on a process main thread the CLI cannot give it, so the board is a
helper subprocess (``python -m vco.float_board --port N [--parent-pid P]``).
The parent-pid watchdog is optional on purpose: a long-lived agent passes its
own pid so the board can never ghost it, while the one-shot `vco board` CLI
omits it so the board survives the command that started it. Unlike the badge,
the board is *driven* while it runs, so the helper also embeds a
loopback-only HTTP server; UI work is funneled onto the AppKit main thread
through PyObjCTools.AppHelper.callAfter (the /health endpoint reports the
model the main thread last applied, which is exactly what the live test uses
to prove cross-thread delivery works).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

from .net import open_url

# 8931 is clear of the panel range (8919-8927), the debug server (8767),
# watch (8766) and ocr-serve (8765).
BOARD_DEFAULT_PORT = 8931

MAX_LINES = 5
MAX_LINE_CHARS = 80
# A line the audience never got to read is worse than a slower board: nothing
# may leave the screen before it has been visible this long.
DWELL_SECONDS = 1.5
LINE_HEIGHT = 40
FONT_SIZE = 28
PADDING = 14
WIDTH_FRACTION = 0.7
EDGE_MARGIN = 60
# Newest first: how opaque each line is by age. A line the audience can no
# longer connect to an on-screen action should not compete with the live one.
LINE_ALPHAS = (1.0, 0.75, 0.55, 0.4, 0.28)


class LineBuffer:
    """The board's retention policy, with no AppKit dependency.

    Pure Python with an injectable clock so the dwell behaviour is unit-testable
    without sleeping. Rules:

    * at most ``max_lines`` lines are visible;
    * a visible line may only be evicted after ``dwell`` seconds on screen —
      a burst of writes must not flash a line for a few milliseconds;
    * pending lines are never dropped silently: everything pushed eventually
      reaches the screen, unless ``clear()`` (or process exit) intervenes.
    """

    def __init__(self, max_lines: int = MAX_LINES, dwell: float = DWELL_SECONDS,
                 clock=time.monotonic):
        self._max = max_lines
        self._dwell = dwell
        self._now = clock
        self._lines: list[str] = []       # visible, oldest first
        self._shown_at: list[float] = []  # parallel to _lines
        self._pending: list[str] = []     # waiting for a dwell-fair slot

    @property
    def lines(self) -> list[str]:
        return list(self._lines)

    @property
    def pending(self) -> int:
        return len(self._pending)

    def push(self, text: str) -> bool:
        self._pending.append(text)
        return self.drain()

    def drain(self) -> bool:
        """Admit every pending line whose eviction is dwell-fair.

        Returns True when the visible set changed (the caller redraws). Called
        on every push and periodically by a timer, so lines admitted while the
        board is full flow in as their elders pass the dwell threshold.
        """

        changed = False
        while self._pending:
            if len(self._lines) < self._max:
                self._lines.append(self._pending.pop(0))
                self._shown_at.append(self._now())
                changed = True
                continue
            if self._now() - self._shown_at[0] >= self._dwell:
                self._lines.pop(0)
                self._shown_at.pop(0)
                self._lines.append(self._pending.pop(0))
                self._shown_at.append(self._now())
                changed = True
                continue
            break
        return changed

    def clear(self) -> None:
        # Pending goes too: a cleared board must not resurrect stale lines the
        # next time the timer drains.
        self._lines = []
        self._shown_at = []
        self._pending = []


# --- client -----------------------------------------------------------------


class BoardClient:
    """Talks to the board helper. Everything is best-effort: the board is a
    display nicety and must never take the real command down with it, so every
    method swallows its failures into a bool."""

    def __init__(self, port: int = BOARD_DEFAULT_PORT, position: str = "bottom",
                 watchdog: bool = True, screen: int | None = None):
        self.port = int(port)
        self.position = position
        # Which NSScreen (by index into NSScreen.screens()) the board sits on.
        # None = the macOS main screen; multi-display recordings need the board
        # on the screen being captured, which is not always the main one.
        self.screen = screen
        # watchdog=True ties the helper's life to this process (right for a
        # long-lived agent driving the board). A one-shot `vco board` CLI must
        # pass False, or the board dies with the command that started it.
        self.watchdog = watchdog
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
        """Is a board already serving on this port? A probe only — never spawns."""
        if not self._supported:
            return False
        answer = self._request("GET", "/health", timeout=1.0)
        return bool(answer and answer.get("ok"))

    def ensure_running(self, timeout: float = 6.0) -> bool:
        """Health-check, and only if that fails spawn the helper and wait."""
        if not self._supported:
            return False
        try:
            import AppKit  # noqa: F401
        except ImportError:
            return False
        if self.healthy():
            return True
        argv = [
            sys.executable, "-m", "vco.float_board",
            "--port", str(self.port),
            "--position", self.position,
        ]
        if self.watchdog:
            argv += ["--parent-pid", str(os.getpid())]
        if self.screen is not None:
            argv += ["--screen", str(self.screen)]
        try:
            subprocess.Popen(
                argv,
                # The helper's output would interleave with the CLI's JSON.
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.healthy():
                return True
            time.sleep(0.15)
        return False

    def write(self, text: str) -> bool:
        answer = self._request("POST", "/write", {"text": str(text)})
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


def describe_action(action) -> str:
    """One short board line for a ResolvedAction.

    The audience needs the gesture, not the payload: coordinates and key names
    go on the board, typed text is truncated so a password or a paragraph
    never fills the overlay (or the recording).
    """

    kind = getattr(action, "type", "?")
    start = getattr(action, "start", None)
    at = f" ({start[0]},{start[1]})" if start else ""
    if kind == "click":
        clicks = getattr(action, "clicks", 1)
        button = getattr(action, "button", "left")
        prefix = "click" if button == "left" else f"{button} click"
        if clicks and clicks > 1:
            prefix += f" ×{clicks}"
        return prefix + at
    if kind == "drag":
        end = getattr(action, "end", None)
        to = f" → ({end[0]},{end[1]})" if end else ""
        return f"drag{at}{to}"
    if kind == "scroll":
        amount = getattr(action, "amount", 0)
        direction = "down" if amount > 0 else "up"
        return f"scroll {direction} {abs(amount)}{at}"
    if kind == "type":
        text = getattr(action, "text", "") or ""
        shown = text[:20] + ("…" if len(text) > 20 else "")
        return "type " + shown
    if kind == "key":
        return "key " + "+".join(getattr(action, "keys", ()) or ())
    if kind == "wait":
        return f"wait {getattr(action, 'seconds', 0)}s"
    return str(kind)


class BoardExecutor:
    """Narrates a real executor onto the board, if one is already running.

    Wrapping (not subclassing) so this works over any ActionExecutor. The
    health probe is cached after the first failure: when no board is up, a run
    must not pay a connection-timeout per action — and it must never spawn one
    (the board belongs to the person recording, started with `vco board`).

    Board lifetime rule (操作边界): one run clears the board exactly once,
    when its first action confirms a board is up — so this run's narration
    starts clean — and never clears/hides/stops it again. The narration must
    stay on screen after the run ends; a mid-run clear or an end-of-run wipe
    would erase lines faster than a human can read them.
    """

    def __init__(self, inner, port: int = BOARD_DEFAULT_PORT,
                 clear_on_start: bool = True):
        self._inner = inner
        self._board = BoardClient(port)
        self._clear_on_start = clear_on_start
        self._online: bool | None = None

    def execute(self, action) -> None:
        self._inner.execute(action)
        if self._online is False:
            return
        if self._online is None:
            self._online = self._board.healthy()
            if not self._online:
                return
            if self._clear_on_start:
                self._board.clear()
        self._board.write(describe_action(action))


# --- helper subprocess (the AppKit side) -------------------------------------


def _helper_main(argv: list[str] | None = None) -> int:
    import argparse
    import queue
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    parser = argparse.ArgumentParser(prog="python -m vco.float_board")
    parser.add_argument("--port", type=int, required=True)
    # Optional: with a parent pid the helper dies when the parent does; the
    # one-shot `vco board` CLI deliberately omits it so the board survives the
    # command that started it (it is reaped by `vco board --stop`).
    parser.add_argument("--parent-pid", type=int, default=None)
    parser.add_argument("--position", choices=("bottom", "top"), default="bottom")
    parser.add_argument("--screen", type=int, default=None)
    args = parser.parse_args(argv)

    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSBackingStoreBuffered,
        NSBezierPath,
        NSColor,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSMakePoint,
        NSMakeRect,
        NSPanel,
        NSScreen,
        NSStatusWindowLevel,
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
    # Accessory policy: no Dock icon, no menu bar — this is an overlay, not an app.
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    font = NSFont.boldSystemFontOfSize_(FONT_SIZE)

    # The retention policy lives in LineBuffer; this queue only ferries HTTP
    # ops onto the AppKit main thread (via callAfter), where they are applied.
    ops: "queue.Queue[tuple[str, str | None]]" = queue.Queue()
    buffer = LineBuffer(MAX_LINES, DWELL_SECONDS)

    class BoardView(NSView):
        def drawRect_(self, _rect):
            bounds = self.bounds()
            NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.6).set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                bounds, 16, 16).fill()
            lines = buffer.lines
            count = len(lines)
            for i, line in enumerate(lines):
                # lines[-1] is the newest, drawn at the bottom and brightest;
                # older lines sit above it, fading with age.
                age = count - 1 - i
                alpha = LINE_ALPHAS[min(age, len(LINE_ALPHAS) - 1)]
                attrs = {
                    NSFontAttributeName: font,
                    NSForegroundColorAttributeName:
                        NSColor.colorWithCalibratedWhite_alpha_(1.0, alpha),
                }
                y = PADDING + (count - 1 - i) * LINE_HEIGHT
                NSString.stringWithString_(line).drawAtPoint_withAttributes_(
                    NSMakePoint(PADDING + 6, y), attrs)

    screens = NSScreen.screens()
    if args.screen is not None and 0 <= args.screen < len(screens):
        screen_frame = screens[args.screen].visibleFrame()
    else:
        screen_frame = NSScreen.mainScreen().visibleFrame()
    width = screen_frame.size.width * WIDTH_FRACTION

    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, width, PADDING * 2 + LINE_HEIGHT),
        NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered,
        False,
    )
    panel.setLevel_(NSStatusWindowLevel)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setIgnoresMouseEvents_(True)
    panel.setHasShadow_(False)
    panel.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
    )
    view = BoardView.alloc().initWithFrame_(NSMakeRect(0, 0, width, PADDING * 2 + LINE_HEIGHT))
    panel.setContentView_(view)

    def _relayout() -> None:
        height = PADDING * 2 + LINE_HEIGHT * max(1, len(buffer.lines))
        x = screen_frame.origin.x + (screen_frame.size.width - width) / 2.0
        if args.position == "top":
            y = screen_frame.origin.y + screen_frame.size.height - height - EDGE_MARGIN
        else:
            y = screen_frame.origin.y + EDGE_MARGIN
        panel.setFrame_display_(
            NSMakeRect(x, y, width, height), True)

    def _apply(op: str, payload: str | None) -> None:
        if op == "write":
            buffer.push((payload or "")[:MAX_LINE_CHARS])
        elif op == "clear":
            buffer.clear()
        elif op == "show":
            panel.orderFrontRegardless()
        elif op == "hide":
            panel.orderOut_(None)
        elif op == "stop":
            # Delayed: the /stop response must actually reach the client
            # before the process goes away, or the caller reads a reset
            # connection as "stop failed" while the board dies anyway.
            callLater(0.25, lambda: app.terminate_(None))
            return
        _relayout()
        view.setNeedsDisplay_(True)

    def _drain() -> None:
        while True:
            try:
                op, payload = ops.get_nowait()
            except queue.Empty:
                break
            _apply(op, payload)

    def _enqueue(op: str, payload: str | None = None) -> None:
        ops.put((op, payload))
        callAfter(_drain)

    def _dwell_tick(_timer) -> None:
        # Pushes drain what they can at arrival; this tick is what lets the
        # rest in as the oldest visible line passes its dwell time.
        if buffer.drain():
            _relayout()
            view.setNeedsDisplay_(True)

    class BoardHandler(BaseHTTPRequestHandler):
        def _json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                # `lines` is the main thread's applied model, `pending` how
                # many lines are still waiting out their elders' dwell time —
                # together they prove both delivery and the retention policy.
                self._json({"ok": True, "lines": buffer.lines,
                            "pending": buffer.pending})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):  # noqa: N802
            if self.path == "/write":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}")
                    text = str(body.get("text") or "")
                except Exception:  # noqa: BLE001
                    self._json({"error": "bad request"}, 400)
                    return
                _enqueue("write", text)
                self._json({"ok": True})
            elif self.path in ("/clear", "/show", "/hide", "/stop"):
                _enqueue(self.path.lstrip("/"))
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", args.port), BoardHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def _watchdog(_timer) -> None:
        # Same contract as cursor_badge: when there IS a parent, the overlay
        # must never outlive it, or the user is left with a ghost window.
        try:
            os.kill(args.parent_pid, 0)
        except OSError:
            app.terminate_(None)

    if args.parent_pid is not None:
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.5, True, _watchdog)

    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.25, True, _dwell_tick)

    _relayout()
    panel.orderFrontRegardless()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(_helper_main())
