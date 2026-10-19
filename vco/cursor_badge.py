"""A floating ring that follows the real mouse while an agent is driving it.

The desktop channel moves the user's actual pointer (pyautogui), so the user
needs a glanceable signal for "that movement was the agent, not a possessed
mouse". macOS offers a CLI process no reliable way to restyle the system
cursor — NSCursor only applies over windows the process owns, and vco owns
none — so the signal is a borderless, click-through overlay panel that trails
the pointer.

AppKit insists its run loop lives on the process's main thread, which the
CLI's command/executor threads cannot provide. The badge therefore runs as a
tiny helper subprocess (``python -m vco.cursor_badge --parent-pid <pid>``);
:class:`AgentCursorBadge` below is just that subprocess's lifecycle.

One-shot commands (``vco click``, ``vco key`` …) end the instant the action
lands, so a helper that dies with its parent would blink for a fraction of a
second per command — invisible in practice, and nothing at all in a screen
recording. The helper therefore *lingers*: it holds its stdin pipe open and
treats the pipe's EOF (parent exited cleanly, was killed, or crashed) as the
signal to keep following the pointer for ``--linger`` more seconds (default
``DEFAULT_LINGER_SECONDS``), which lets back-to-back commands read as one
continuous agent presence, then terminates on its own so the user is never
left with a ghost ring. The parent-pid watchdog remains as a backstop.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

# Ring geometry (points). The window is larger than the ring so the ring can
# sit offset from the pointer tip instead of on top of it.
WINDOW_SIZE = 44
RING_DIAMETER = 28
RING_LINE_WIDTH = 3.0
# Lower-right of the pointer tip, in screen points.
OFFSET_X = 14
OFFSET_Y = 12

# How long the ring stays up after the owning process exits. Long enough that
# a sequence of one-shot CLI commands reads as one continuous agent presence
# (and shows up in screen recordings), short enough to never feel like a
# ghost. 0 restores the original die-with-parent behaviour.
DEFAULT_LINGER_SECONDS = 3.0


class AgentCursorBadge:
    """Owns the badge helper subprocess. Everything is a no-op off macOS or
    with ``enabled=False`` — the badge is a nicety, never a reason to fail a
    real mouse action."""

    def __init__(self, enabled: bool = True, linger: float = DEFAULT_LINGER_SECONDS):
        self._enabled = enabled
        self._linger = linger
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        if not self._enabled or self._proc is not None:
            return
        if sys.platform != "darwin":
            return
        try:
            import AppKit  # noqa: F401
        except ImportError:
            # Without PyObjC the helper cannot run; say so once and go quiet.
            print("vco: cursor badge unavailable (AppKit not importable)", file=sys.stderr)
            self._enabled = False
            return
        try:
            self._proc = subprocess.Popen(
                [
                    sys.executable, "-m", "vco.cursor_badge",
                    "--parent-pid", str(os.getpid()),
                    "--linger", str(self._linger),
                ],
                # stdin is the lifeline: the helper lingers once it sees EOF,
                # which happens on clean exit, kill, and crash alike.
                stdin=subprocess.PIPE,
                # The helper's own stdout/stderr would interleave with the
                # CLI's JSON output, which callers parse.
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            print(f"vco: cursor badge failed to start ({exc})", file=sys.stderr)
            self._proc = None

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        # Do NOT terminate or wait: closing stdin is the "wrap up" signal —
        # the helper keeps the ring up for the linger window and then exits
        # on its own. Terminating would make one-shot commands invisible;
        # waiting would delay every CLI exit by the linger window.
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "AgentCursorBadge":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


# --- helper subprocess (runs the AppKit side) --------------------------------


def _helper_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m vco.cursor_badge")
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument(
        "--linger", type=float, default=DEFAULT_LINGER_SECONDS,
        help="seconds to keep following the pointer after the parent exits",
    )
    args = parser.parse_args(argv)

    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSBackingStoreBuffered,
        NSBezierPath,
        NSColor,
        NSEvent,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSMakePoint,
        NSMakeRect,
        NSPanel,
        NSStatusWindowLevel,
        NSTimer,
        NSView,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorStationary,
        NSWindowStyleMaskBorderless,
        NSWindowStyleMaskNonactivatingPanel,
    )
    from Foundation import NSString

    import threading

    app = NSApplication.sharedApplication()
    # Accessory policy: no Dock icon, no menu bar — this is an overlay, not an app.
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    # stdin EOF = the parent exited (cleanly, killed, or crashed — the pipe
    # closes in every case) or explicitly stopped us. A blocked read on a
    # daemon thread is the portable way to notice; the run loop owns main.
    stdin_closed = threading.Event()

    def _watch_stdin() -> None:
        try:
            sys.stdin.buffer.read()
        except Exception:  # noqa: BLE001
            pass
        stdin_closed.set()

    threading.Thread(target=_watch_stdin, daemon=True).start()

    text_attrs = {
        NSFontAttributeName: NSFont.boldSystemFontOfSize_(7.0),
        NSForegroundColorAttributeName: NSColor.systemOrangeColor(),
    }

    class BadgeView(NSView):
        def drawRect_(self, _rect):
            margin = (WINDOW_SIZE - RING_DIAMETER) / 2.0
            ring = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(margin, margin, RING_DIAMETER, RING_DIAMETER)
            )
            NSColor.systemOrangeColor().set()
            ring.setLineWidth_(RING_LINE_WIDTH)
            ring.stroke()
            # A small filled dot inside the ring, so the badge still reads at a
            # glance when the ring overlaps an orange-ish page.
            dot = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(WINDOW_SIZE / 2.0 - 3, WINDOW_SIZE / 2.0 + 2, 6, 6)
            )
            dot.fill()
            NSString.stringWithString_("agent").drawAtPoint_withAttributes_(
                NSMakePoint(6, 4), text_attrs
            )

    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, WINDOW_SIZE, WINDOW_SIZE),
        NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered,
        False,
    )
    panel.setLevel_(NSStatusWindowLevel)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setIgnoresMouseEvents_(True)
    panel.setHasShadow_(False)
    # Follow the pointer across Spaces and over fullscreen windows.
    panel.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
    )
    panel.setContentView_(
        BadgeView.alloc().initWithFrame_(NSMakeRect(0, 0, WINDOW_SIZE, WINDOW_SIZE))
    )
    panel.orderFrontRegardless()

    parent_dead_since: list[float] = []

    def _tick(_timer) -> None:
        # The watchdog matters more than the animation: the badge must never
        # turn into a ghost ring the user cannot kill. Parent gone (stdin EOF
        # or pid vanished) starts a short linger countdown — back-to-back
        # one-shot commands read as one continuous presence — and when it
        # runs out, the helper terminates itself.
        gone = stdin_closed.is_set()
        if not gone:
            try:
                os.kill(args.parent_pid, 0)
            except OSError:
                gone = True
        if gone:
            if args.linger <= 0:
                app.terminate_(None)
                return
            if not parent_dead_since:
                parent_dead_since.append(time.monotonic())
            elif time.monotonic() - parent_dead_since[0] >= args.linger:
                app.terminate_(None)
                return
        else:
            parent_dead_since.clear()
        loc = NSEvent.mouseLocation()
        panel.setFrameOrigin_(
            NSMakePoint(loc.x + OFFSET_X, loc.y - WINDOW_SIZE - OFFSET_Y)
        )

    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.016, True, _tick)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(_helper_main())
