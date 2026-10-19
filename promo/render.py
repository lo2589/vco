#!/usr/bin/env python3
"""Render promo/index.html frame-by-frame and mux with audio.wav into an mp4.

Serves the repo root over loopback (the page references /examples, /debug,
/assets), steps the deterministic frame clock, screenshots every frame, then
hands off to ffmpeg.

    .venv/bin/python promo/render.py                 # full 48 s @ 30 fps
    .venv/bin/python promo/render.py --seconds 6     # quick preview of the intro
"""
import argparse
import functools
import http.server
import subprocess
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PROMO = ROOT / "promo"
FRAMES = PROMO / "frames"


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(port):
    handler = functools.partial(Quiet, directory=str(ROOT))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--seconds", type=float, default=None, help="render only the first N seconds")
    ap.add_argument("--scale", type=int, default=2, help="device scale factor (2 = 2560x1440)")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--keep-frames", action="store_true")
    ap.add_argument("--output", default=str(PROMO / "vco-promo.mp4"))
    args = ap.parse_args()

    audio = PROMO / "audio.wav"
    if not audio.exists():
        sys.exit("promo/audio.wav missing — run promo/music.py first")

    FRAMES.mkdir(exist_ok=True)
    for old in FRAMES.glob("f*.png"):
        old.unlink()

    httpd = serve(args.port)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(
                viewport={"width": 1280, "height": 720},
                device_scale_factor=args.scale,
            )
            page.goto(f"http://127.0.0.1:{args.port}/promo/index.html#capture")
            page.wait_for_function("window.__READY === true", timeout=30000)
            total = int(page.evaluate("window.VCO_TOTAL_FRAMES"))
            if args.seconds is not None:
                total = min(total, int(args.seconds * args.fps))
            print(f"rendering {total} frames @ {args.fps} fps ...")
            for n in range(total):
                page.evaluate(f"window.__setFrame({n})")
                page.screenshot(path=str(FRAMES / f"f{n:05d}.png"))
                if n % 90 == 0 or n == total - 1:
                    print(f"  frame {n}/{total}", flush=True)
            browser.close()
    finally:
        httpd.shutdown()

    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(args.fps),
        "-i", str(FRAMES / "f%05d.png"),
        "-i", str(audio),
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart",
        args.output,
    ]
    subprocess.run(cmd, check=True)
    if not args.keep_frames:
        for f in FRAMES.glob("f*.png"):
            f.unlink()
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
