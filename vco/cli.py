"""Command-line interface for offline inspection and the live loop."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageGrab

from .adaptive_zoom import AdaptiveZoomProvider
from .capture import PillowScreenCapture
from .diff import make_diff_image, verify_target_stable
from .executor import (
    DryRunExecutor,
    PyAutoGuiExecutor,
    ResolvedAction,
    resolve_action,
)
from .geometry import GridMapper
from .grid import render_numbered_grid
from .loop import ComputerUseLoop
from .models import GridSpec, Region, parse_action
from .ocr import OCRRequest, create_ocr_backend
from .ocr_assist import OCRAssistProvider
from .ocr_server import serve_ocr
from .providers import (
    GLMProvider,
    ManualProvider,
    MiniMaxProvider,
    OllamaProvider,
    OpenAIProvider,
    ReplayProvider,
)
from .zoom import ZoomProvider


def _region(value: str) -> Region:
    try:
        x, y, width, height = (int(part.strip()) for part in value.split(","))
        return Region(x=x, y=y, width=width, height=height)
    except Exception as exc:
        raise argparse.ArgumentTypeError("region must be x,y,width,height") from exc


def _point(value: str) -> tuple[int, int]:
    try:
        x, y = (int(part.strip()) for part in value.split(","))
        return (x, y)
    except Exception as exc:
        raise argparse.ArgumentTypeError("point must be x,y") from exc


def _grid(args) -> GridSpec:
    if getattr(args, "zoom", False):
        return GridSpec(rows=4, cols=4)
    default_size = 16 if getattr(args, "provider", None) in {"minimax", "glm"} else 10
    return GridSpec(
        rows=args.rows if args.rows is not None else default_size,
        cols=args.cols if args.cols is not None else default_size,
    )


def _add_grid_args(parser):
    parser.add_argument("--rows", type=int)
    parser.add_argument("--cols", type=int)


def _add_provider_args(parser, *, default="manual"):
    parser.add_argument(
        "--provider",
        choices=("ollama", "openai", "minimax", "glm", "manual", "replay", "none"),
        default=default,
    )
    parser.add_argument("--model", help="model ID; MiniMax defaults to MiniMax-M3")
    parser.add_argument("--replay", type=Path, help="JSONL for replay provider")
    parser.add_argument(
        "--ollama-url", default="http://127.0.0.1:11434", help="Ollama base URL"
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--minimax-settings",
        type=Path,
        help="JSON provider settings containing api_key",
    )
    parser.add_argument(
        "--minimax-url",
        default="https://api.minimaxi.com/anthropic",
        help="MiniMax Anthropic-compatible base URL",
    )
    parser.add_argument(
        "--minimax-service-tier",
        choices=("standard", "priority"),
        default="priority",
    )
    parser.add_argument(
        "--minimax-image-detail",
        choices=("low", "default", "high"),
        default="default",
    )
    parser.add_argument(
        "--minimax-image-mode",
        choices=("both", "clean", "grid"),
        default="both",
        help="send clean+grid views or only the numbered view",
    )
    parser.add_argument(
        "--glm-settings",
        type=Path,
        help="JSON provider settings containing GLM api_key and base_url",
    )
    parser.add_argument(
        "--glm-thinking",
        choices=("disabled", "enabled"),
        default="disabled",
    )
    parser.add_argument(
        "--glm-image-mode",
        choices=("both", "clean", "grid"),
        default="both",
    )
    parser.add_argument(
        "--adaptive-zoom",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="let MiniMax/GLM click now or request another zoom level",
    )
    parser.add_argument("--adaptive-zoom-levels", type=int, default=3)
    parser.add_argument("--adaptive-zoom-span", type=float, default=4.0)
    parser.add_argument(
        "--adaptive-zoom-strategy",
        choices=("model", "fixed"),
        default="model",
        help="let the model stop early or always refine through every level",
    )
    parser.add_argument(
        "--ollama-image-mode",
        choices=("both", "grid"),
        default="both",
        help="send both views or only the numbered view to small local models",
    )
    parser.add_argument(
        "--zoom",
        action="store_true",
        help="use 4x4 -> nearby 2x2 -> 4x4 hierarchical selection",
    )
    parser.add_argument(
        "--zoom-use-model-offset",
        action="store_true",
        help="use the fine model offset instead of the fine cell center",
    )
    parser.add_argument(
        "--zoom-center-delta",
        action="store_true",
        help="ask for signed pixel displacement from the centered fine-grid number",
    )
    parser.add_argument(
        "--ocr-backend",
        help="enable OCR with auto, rapidocr, http, apple-vision, or a registered backend",
    )
    parser.add_argument("--ocr-url", help="JSON endpoint required by --ocr-backend http")
    parser.add_argument(
        "--ocr-api-key-env",
        help="environment variable containing the HTTP OCR bearer token",
    )
    parser.add_argument(
        "--ocr-target",
        help="exact/substring text target; one OCR match bypasses the vision model",
    )
    parser.add_argument(
        "--ocr-match",
        choices=("exact", "contains"),
        default="exact",
    )
    parser.add_argument(
        "--ocr-mode",
        choices=("fast", "accurate"),
        default="accurate",
    )
    parser.add_argument("--ocr-language", action="append", dest="ocr_languages")
    parser.add_argument("--ocr-custom-word", action="append", default=[])
    parser.add_argument(
        "--ocr-hint-text",
        action="append",
        default=[],
        help="send only OCR observations containing this text to the vision model",
    )
    parser.add_argument("--ocr-min-confidence", type=float, default=0.5)
    parser.add_argument(
        "--no-ocr-direct-click",
        action="store_true",
        help="send OCR hints to the vision model even for one exact match",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vco",
        description="A clicker for LLMs: operate web pages and desktop screens via shell commands.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    overlay = commands.add_parser("overlay", help="draw a grid over an image")
    overlay.add_argument("image", type=Path)
    overlay.add_argument("--output", type=Path, required=True)
    overlay.add_argument("--mapping", type=Path)
    overlay.add_argument("--region", type=_region)
    _add_grid_args(overlay)

    convert = commands.add_parser("convert", help="resolve model JSON locally")
    convert.add_argument("--region", type=_region, required=True)
    convert.add_argument("--action", required=True, help="JSON object")
    _add_grid_args(convert)

    ocr = commands.add_parser("ocr", help="run a local OCR backend on an image")
    ocr.add_argument("image", type=Path)
    ocr.add_argument(
        "--backend",
        default="auto",
        help="registered OCR backend name (default: auto)",
    )
    ocr.add_argument("--url", help="endpoint required by --backend http")
    ocr.add_argument(
        "--api-key-env", help="environment variable containing HTTP bearer token"
    )
    ocr.add_argument("--mode", default="accurate", choices=("fast", "accurate"))
    ocr.add_argument("--language", action="append", dest="languages")
    ocr.add_argument("--custom-word", action="append", default=[])
    ocr.add_argument("--min-confidence", type=float, default=0.0)
    ocr.add_argument("--timeout", type=float, default=30.0)
    ocr.add_argument("--output", type=Path)

    ocr_serve = commands.add_parser(
        "ocr-serve", help="serve the portable JSON OCR API"
    )
    ocr_serve.add_argument("--backend", default="rapidocr")
    ocr_serve.add_argument("--host", default="127.0.0.1")
    ocr_serve.add_argument("--port", type=int, default=8765)
    ocr_serve.add_argument("--timeout", type=float, default=30.0)
    ocr_serve.add_argument(
        "--api-key-env", help="environment variable containing required bearer token"
    )

    probe = commands.add_parser(
        "probe", help="ask a vision model about a static image without mouse input"
    )
    probe.add_argument("--task", required=True)
    probe.add_argument("--image", type=Path, required=True)
    probe.add_argument("--output-grid", type=Path)
    probe.add_argument("--output-json", type=Path)
    probe.add_argument("--region", type=_region)
    _add_provider_args(probe, default="ollama")
    _add_grid_args(probe)

    run = commands.add_parser("run", help="run the screenshot/action loop")
    run.add_argument("--task", required=True)
    run.add_argument("--region", type=_region, required=True)
    _add_provider_args(run)
    run.add_argument("--execute", action="store_true", help="enable real mouse input")
    run.add_argument("--max-steps", type=int, default=12)
    run.add_argument("--settle", type=float, default=0.7)
    run.add_argument("--artifacts", type=Path, default=Path("cache/runs"))
    _add_grid_args(run)

    shot = commands.add_parser(
        "shot", help="capture the screen into ./.screenshot/ (LLM-friendly)"
    )
    shot.add_argument("--region", type=_region, help="x,y,width,height; default full screen")
    shot.add_argument(
        "--display", type=int, default=1, help="display index; 1 = main (default)"
    )
    shot.add_argument(
        "--dir",
        type=Path,
        default=Path(".screenshot"),
        help="artifact directory (default: .screenshot under the current directory)",
    )

    gridshot = commands.add_parser(
        "gridshot", help="capture the screen and render a numbered grid over it"
    )
    gridshot.add_argument("--region", type=_region, help="x,y,width,height; default full screen")
    gridshot.add_argument(
        "--display", type=int, default=1, help="display index; 1 = main (default)"
    )
    gridshot.add_argument(
        "--dir",
        type=Path,
        default=Path(".screenshot"),
        help="artifact directory (default: .screenshot under the current directory)",
    )
    _add_grid_args(gridshot)

    extendgrid = commands.add_parser(
        "extendgrid", help="zoom into a grid cell and re-render a finer numbered grid"
    )
    extendgrid.add_argument("--clean", type=Path, required=True, help="previous gridshot clean image")
    extendgrid.add_argument(
        "--cell", type=int, required=True, help="cell number to zoom into"
    )
    extendgrid.add_argument(
        "--from-rows", type=int, required=True, help="rows of the previous grid"
    )
    extendgrid.add_argument(
        "--from-cols", type=int, required=True, help="cols of the previous grid"
    )
    extendgrid.add_argument(
        "--dir",
        type=Path,
        default=Path(".screenshot"),
        help="artifact directory (default: .screenshot under the current directory)",
    )
    _add_grid_args(extendgrid)

    find = commands.add_parser(
        "find",
        help="locate a target (OCR first, vision model fallback), mark it, never click",
    )
    find.add_argument("--target", help="on-screen text to locate via OCR first")
    find.add_argument("--task", help="task for the vision model; derived from --target")
    find.add_argument("--image", type=Path, help="use this image instead of capturing")
    find.add_argument("--region", type=_region, help="x,y,width,height; default full screen")
    find.add_argument("--display", type=int, default=1, help="display index; 1 = main (default)")
    find.add_argument("--dir", type=Path, default=Path(".screenshot"))
    _add_provider_args(find, default="none")
    _add_grid_args(find)

    click = commands.add_parser(
        "click", help="like find, but performs a real mouse click"
    )
    click.add_argument("--target", help="on-screen text to locate via OCR first")
    click.add_argument("--task", help="task for the vision model; derived from --target")
    click.add_argument("--at", type=_point, help="click x,y directly, skip locating")
    click.add_argument("--image", type=Path, help="locate on this image instead of capturing")
    click.add_argument("--region", type=_region, help="x,y,width,height; default full screen")
    click.add_argument("--display", type=int, default=1, help="display index; 1 = main (default)")
    click.add_argument("--dir", type=Path, default=Path(".screenshot"))
    click.add_argument(
        "--no-click-trace",
        action="store_true",
        help="do not save a post-click screenshot with a marker",
    )
    click.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the pre-click region-stability check",
    )
    click.add_argument(
        "--verify-threshold",
        type=float,
        default=0.8,
        help="minimum pixel similarity for the target region (default 0.8)",
    )
    _add_provider_args(click, default="none")
    _add_grid_args(click)

    type_cmd = commands.add_parser(
        "type", help="locate a text field and type/paste text into it"
    )
    type_cmd.add_argument("--target", help="field label to locate via OCR")
    type_cmd.add_argument("--at", type=_point, help="click x,y directly, skip locating")
    type_cmd.add_argument("--text", help="text to input")
    type_cmd.add_argument(
        "--text-env",
        help="environment variable containing the text (safer than --text for secrets)",
    )
    type_cmd.add_argument(
        "--paste",
        action="store_true",
        help="paste from clipboard instead of simulating keystrokes",
    )
    type_cmd.add_argument(
        "--press-enter",
        type=int,
        default=0,
        help="press Enter N times after typing (useful for tag inputs)",
    )
    type_cmd.add_argument("--image", type=Path, help="use this image instead of capturing")
    type_cmd.add_argument("--region", type=_region, help="x,y,width,height; default full screen")
    type_cmd.add_argument("--display", type=int, default=1, help="display index; 1 = main (default)")
    type_cmd.add_argument("--dir", type=Path, default=Path(".screenshot"))
    type_cmd.add_argument(
        "--settle", type=float, default=0.3, help="seconds to wait after focus click before typing"
    )
    _add_provider_args(type_cmd, default="none")
    _add_grid_args(type_cmd)

    fill = commands.add_parser(
        "fill", help="fill multiple form fields, then optionally click a submit button"
    )
    fill.add_argument(
        "--field",
        action="append",
        required=True,
        help="label=text pair to fill, e.g. --field 用户名=alice (can be repeated)",
    )
    fill.add_argument("--target", help="submit button text to click after filling")
    fill.add_argument(
        "--paste",
        action="store_true",
        help="paste values instead of simulating keystrokes",
    )
    fill.add_argument(
        "--press-enter",
        type=int,
        default=0,
        help="press Enter N times after each field (useful for tag inputs)",
    )
    fill.add_argument("--image", type=Path, help="use this image instead of capturing")
    fill.add_argument("--region", type=_region, help="x,y,width,height; default full screen")
    fill.add_argument("--display", type=int, default=1, help="display index; 1 = main (default)")
    fill.add_argument("--dir", type=Path, default=Path(".screenshot"))
    fill.add_argument(
        "--settle", type=float, default=0.3, help="seconds to wait after each focus click"
    )
    _add_provider_args(fill, default="none")
    _add_grid_args(fill)

    ask = commands.add_parser(
        "ask", help="ask a vision model a free-form question about an image"
    )
    ask.add_argument(
        "image", type=Path, nargs="?", help="image file; omit to capture the screen"
    )
    ask.add_argument(
        "--question", default="图里有什么？请简要描述你看到的内容。"
    )
    ask.add_argument(
        "--provider", choices=("ollama", "openai", "minimax", "glm"), required=True
    )
    ask.add_argument("--model", help="model ID; MiniMax defaults to MiniMax-M3")
    ask.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    ask.add_argument("--timeout", type=float, default=180.0)
    ask.add_argument("--minimax-settings", type=Path)
    ask.add_argument(
        "--minimax-url", default="https://api.minimaxi.com/anthropic"
    )
    ask.add_argument(
        "--minimax-service-tier", choices=("standard", "priority"), default="priority"
    )
    ask.add_argument(
        "--minimax-image-detail", choices=("low", "default", "high"), default="default"
    )
    ask.add_argument("--glm-settings", type=Path)
    ask.add_argument(
        "--glm-thinking", choices=("disabled", "enabled"), default="disabled"
    )
    ask.add_argument("--dir", type=Path, default=Path(".screenshot"))
    ask.add_argument("--display", type=int, default=1, help="display index; 1 = main (default)")

    webshot = commands.add_parser(
        "webshot", help="render a URL in headless Chromium and screenshot it"
    )
    webshot.add_argument("url")
    webshot.add_argument("--output", type=Path, help="default: .screenshot/webshot-<ts>.png")
    webshot.add_argument("--full-page", action="store_true", help="capture the full scrollable page")
    webshot.add_argument("--width", type=int, default=1280)
    webshot.add_argument("--height", type=int, default=800)
    webshot.add_argument("--settle", type=float, default=0.0, help="extra wait in seconds after load")
    webshot.add_argument("--timeout", type=float, default=15.0)
    webshot.add_argument("--dir", type=Path, default=Path(".screenshot"))
    webshot.add_argument("--profile", type=Path, help="persistent browser profile dir (keeps login/cookies)")
    webshot.add_argument("--headed", action="store_true", help="show the browser window")
    webshot.add_argument("--hold", type=float, default=0.0, help="keep the window open N seconds after finishing")
    webshot.add_argument("--record", action="store_true", help="record a .webm video of the session")

    webclick = commands.add_parser(
        "webclick", help="DOM-click a text target on a page in headless Chromium"
    )
    webclick.add_argument("url")
    webclick.add_argument("--target", help="visible text to click")
    webclick.add_argument(
        "--selector", help="CSS/Playwright selector to click instead of --target"
    )
    webclick.add_argument(
        "--contains", action="store_true", help="substring match instead of exact"
    )
    webclick.add_argument("--settle", type=float, default=0.5)
    webclick.add_argument("--timeout", type=float, default=15.0)
    webclick.add_argument("--width", type=int, default=1280)
    webclick.add_argument("--height", type=int, default=800)
    webclick.add_argument("--dir", type=Path, default=Path(".screenshot"))
    webclick.add_argument(
        "--fill",
        action="append",
        default=[],
        help="fill an input before clicking, format: placeholder=text (repeatable)",
    )
    webclick.add_argument("--profile", type=Path, help="persistent browser profile dir (keeps login/cookies)")
    webclick.add_argument("--headed", action="store_true", help="show the browser window")
    webclick.add_argument("--hold", type=float, default=0.0, help="keep the window open N seconds after finishing")
    webclick.add_argument(
        "--expect", help="after clicking, wait until this text is visible (verified field)"
    )
    webclick.add_argument("--expect-timeout", type=float, default=10.0)
    webclick.add_argument("--record", action="store_true", help="record a .webm video of the session")

    webtext = commands.add_parser(
        "webtext", help="dump a page's accessibility tree as text (no vision needed)"
    )
    webtext.add_argument("url")
    webtext.add_argument("--settle", type=float, default=0.0)
    webtext.add_argument("--timeout", type=float, default=15.0)
    webtext.add_argument("--width", type=int, default=1280)
    webtext.add_argument("--height", type=int, default=800)
    webtext.add_argument("--profile", type=Path, help="persistent browser profile dir (keeps login/cookies)")
    webtext.add_argument("--headed", action="store_true", help="show the browser window")
    webtext.add_argument("--hold", type=float, default=0.0, help="keep the window open N seconds after finishing")

    webdebug = commands.add_parser(
        "webdebug",
        help="diagnose a front-end bug: reproduce it, record why, report; never edits",
    )
    webdebug.add_argument("url")
    webdebug.add_argument("--task", required=True, help="the complaint, in a user's words")
    webdebug.add_argument(
        "--provider", required=True, choices=["ollama", "openai", "minimax", "glm"]
    )
    webdebug.add_argument("--model")
    webdebug.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    webdebug.add_argument("--timeout", type=float, default=180.0)
    webdebug.add_argument("--minimax-settings", type=Path)
    webdebug.add_argument("--minimax-url", default="https://api.minimaxi.com/anthropic")
    webdebug.add_argument("--minimax-service-tier", choices=["standard", "priority"], default="standard")
    webdebug.add_argument("--minimax-image-detail", choices=["low", "default", "high"], default="default")
    webdebug.add_argument("--glm-settings", type=Path)
    webdebug.add_argument("--glm-thinking", choices=["disabled", "enabled"], default="disabled")
    webdebug.add_argument("--max-steps", type=int, default=12)
    webdebug.add_argument("--settle", type=float, default=0.7)
    webdebug.add_argument("--width", type=int, default=1280)
    webdebug.add_argument("--height", type=int, default=800)
    webdebug.add_argument("--profile", type=Path)
    webdebug.add_argument("--headed", action="store_true", help="show the browser window")
    webdebug.add_argument("--hold", type=float, default=0.0)
    webdebug.add_argument("--record", action="store_true")
    webdebug.add_argument("--dir", type=Path)

    webrun = commands.add_parser(
        "webrun", help="agent loop: text model drives a page via aria snapshots"
    )
    webrun.add_argument("url")
    webrun.add_argument("--task", required=True)
    webrun.add_argument(
        "--provider", choices=("ollama", "openai", "minimax", "glm"), required=True
    )
    webrun.add_argument("--model", help="model ID; MiniMax defaults to MiniMax-M3")
    webrun.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    webrun.add_argument("--timeout", type=float, default=180.0)
    webrun.add_argument("--minimax-settings", type=Path)
    webrun.add_argument("--minimax-url", default="https://api.minimaxi.com/anthropic")
    webrun.add_argument(
        "--minimax-service-tier", choices=("standard", "priority"), default="priority"
    )
    webrun.add_argument(
        "--minimax-image-detail", choices=("low", "default", "high"), default="default"
    )
    webrun.add_argument("--glm-settings", type=Path)
    webrun.add_argument(
        "--glm-thinking", choices=("disabled", "enabled"), default="disabled"
    )
    webrun.add_argument("--max-steps", type=int, default=8)
    webrun.add_argument("--settle", type=float, default=0.7)
    webrun.add_argument("--width", type=int, default=1280)
    webrun.add_argument("--height", type=int, default=800)
    webrun.add_argument("--profile", type=Path)
    webrun.add_argument("--headed", action="store_true", help="show the browser window")
    webrun.add_argument("--hold", type=float, default=0.0)
    webrun.add_argument("--record", action="store_true", help="record a .webm video of the session")
    webrun.add_argument("--dir", type=Path, default=Path(".screenshot"))
    return parser


class _NoVisionProvider:
    """Placeholder used when OCR is the only locating method available."""

    def choose_action(self, **kwargs):
        raise RuntimeError(
            "no vision provider configured and OCR could not uniquely locate "
            "the target; pass --provider ollama/minimax/glm ... as a fallback"
        )


def _base_provider(args):
    """Build the raw vision provider without OCR or zoom wrappers."""
    if args.provider == "none":
        return _NoVisionProvider()
    if args.provider == "manual":
        return ManualProvider()
    if args.provider == "replay":
        if args.replay is None:
            raise SystemExit("--provider replay requires --replay FILE")
        return ReplayProvider(args.replay)
    if args.provider == "ollama":
        if not args.model:
            raise SystemExit("--provider ollama requires --model MODEL")
        return OllamaProvider(
            args.model,
            base_url=args.ollama_url,
            timeout=args.timeout,
            image_mode=getattr(args, "ollama_image_mode", "both"),
        )
    if args.provider == "minimax":
        if args.minimax_settings is None:
            raise SystemExit("--provider minimax requires --minimax-settings FILE")
        try:
            settings = json.loads(args.minimax_settings.read_text(encoding="utf-8"))
            api_key = settings["api_key"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise SystemExit(f"cannot read MiniMax api_key from settings: {exc}") from exc
        return MiniMaxProvider(
            args.model or "MiniMax-M3",
            api_key=api_key,
            base_url=args.minimax_url,
            timeout=args.timeout,
            service_tier=args.minimax_service_tier,
            image_detail=args.minimax_image_detail,
            image_mode=getattr(args, "minimax_image_mode", "both"),
        )
    if args.provider == "glm":
        if args.glm_settings is None:
            raise SystemExit("--provider glm requires --glm-settings FILE")
        try:
            settings = json.loads(args.glm_settings.read_text(encoding="utf-8"))
            api_key = settings["api_key"]
            base_url = settings.get(
                "base_url", "https://open.bigmodel.cn/api/paas/v4/"
            )
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise SystemExit(f"cannot read GLM settings: {exc}") from exc
        return GLMProvider(
            args.model or "glm-4.6v",
            api_key=api_key,
            base_url=base_url,
            timeout=args.timeout,
            thinking=args.glm_thinking,
            image_mode=getattr(args, "glm_image_mode", "both"),
        )
    if not args.model:
        raise SystemExit("--provider openai requires an explicit --model MODEL")
    return OpenAIProvider(args.model)


def _provider(args):
    if args.zoom_use_model_offset and args.zoom_center_delta:
        raise SystemExit(
            "--zoom-use-model-offset and --zoom-center-delta are mutually exclusive"
        )
    if args.provider in {"minimax", "glm"} and args.zoom:
        raise SystemExit("MiniMax/GLM use adaptive 16x16 zoom; omit --zoom")
    provider = _base_provider(args)
    if args.ocr_target and not args.ocr_backend:
        raise SystemExit("--ocr-target requires --ocr-backend")
    if args.ocr_backend:
        try:
            ocr_options = {}
            if args.ocr_backend == "http":
                if not args.ocr_url:
                    raise ValueError("--ocr-backend http requires --ocr-url")
                ocr_options["endpoint"] = args.ocr_url
                if args.ocr_api_key_env:
                    api_key = os.environ.get(args.ocr_api_key_env)
                    if not api_key:
                        raise ValueError(
                            f"environment variable {args.ocr_api_key_env!r} is empty"
                        )
                    ocr_options["api_key"] = api_key
            ocr_backend = create_ocr_backend(
                args.ocr_backend, timeout=args.timeout, **ocr_options
            )
            ocr_request = OCRRequest(
                languages=tuple(args.ocr_languages or ("zh-Hans", "en-US")),
                mode=args.ocr_mode,
                custom_words=tuple(args.ocr_custom_word),
                min_confidence=args.ocr_min_confidence,
            )
        except (RuntimeError, ValueError) as exc:
            raise SystemExit(f"cannot initialize OCR: {exc}") from exc
        provider = OCRAssistProvider(
            provider,
            ocr_backend,
            request=ocr_request,
            target_text=args.ocr_target,
            exact_match=args.ocr_match == "exact",
            direct_click=not args.no_ocr_direct_click,
            hint_texts=tuple(args.ocr_hint_text),
        )
    if args.provider == "none" and args.zoom:
        raise SystemExit("--provider none has no vision model; omit --zoom")
    if args.provider in {"minimax", "glm"} and args.adaptive_zoom:
        provider = AdaptiveZoomProvider(
            provider,
            max_levels=args.adaptive_zoom_levels,
            span_cells=args.adaptive_zoom_span,
            force_initial_click=args.command in ("probe", "find", "click"),
            always_refine=args.adaptive_zoom_strategy == "fixed",
        )
    if args.zoom:
        provider = ZoomProvider(
            provider,
            use_model_offset=args.zoom_use_model_offset,
            use_center_delta=args.zoom_center_delta,
            force_initial_click=args.command in ("probe", "find", "click"),
        )
    return provider


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]


def _display_bounds() -> list[tuple[int, int, int, int]]:
    """Logical (x, y, w, h) bounds of each active display, main first (macOS)."""
    if sys.platform != "darwin":
        return []
    try:
        import ctypes

        class _CGRect(ctypes.Structure):
            _fields_ = [
                ("x", ctypes.c_double),
                ("y", ctypes.c_double),
                ("w", ctypes.c_double),
                ("h", ctypes.c_double),
            ]

        quartz = ctypes.CDLL("/System/Library/Frameworks/Quartz.framework/Quartz")
        quartz.CGDisplayBounds.restype = _CGRect
        quartz.CGDisplayBounds.argtypes = [ctypes.c_uint32]
        ids = (ctypes.c_uint32 * 16)()
        count = ctypes.c_uint32(0)
        if quartz.CGGetActiveDisplayList(16, ids, ctypes.byref(count)) != 0:
            return []
        bounds = []
        for i in range(count.value):
            rect = quartz.CGDisplayBounds(ids[i])
            bounds.append((int(rect.x), int(rect.y), int(rect.w), int(rect.h)))
        return bounds
    except Exception:
        return []


def _capture_image(region: Region | None, display: int = 1) -> Image.Image:
    """Grab a display (or a display-local region), downscaled to logical pixels."""
    bounds = _display_bounds()
    if bounds and not 1 <= display <= len(bounds):
        raise SystemExit(f"--display must be within 1..{len(bounds)}")
    if not bounds and display != 1:
        raise SystemExit("--display > 1 requires macOS display enumeration (failed)")
    if bounds:
        dx, dy, dw, dh = bounds[display - 1]
        import subprocess
        import tempfile

        fd, tmp_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            subprocess.run(
                ["screencapture", "-x", "-D", str(display), tmp_path],
                check=True,
                capture_output=True,
            )
            with Image.open(tmp_path) as raw:
                image = raw.convert("RGB")
        except (subprocess.CalledProcessError, OSError) as exc:
            raise SystemExit(
                "screen capture failed; on macOS grant Screen Recording "
                "permission to the terminal or host app"
            ) from exc
        finally:
            os.unlink(tmp_path)
        if image.size != (dw, dh):
            image = image.resize((dw, dh), Image.LANCZOS)
        if region is not None:
            if not (0 <= region.x and 0 <= region.y
                    and region.x + region.width <= dw
                    and region.y + region.height <= dh):
                raise SystemExit(
                    f"--region must be inside display {display} "
                    f"(0,0,{dw},{dh}); got {region.x},{region.y},"
                    f"{region.width},{region.height}"
                )
            image = image.crop(
                (region.x, region.y, region.x + region.width, region.y + region.height)
            )
        return image
    # Non-macOS fallback: Pillow's native grabber, main display only.
    try:
        if region is not None:
            bbox = (region.x, region.y, region.x + region.width, region.y + region.height)
            image = ImageGrab.grab(bbox=bbox).convert("RGB")
            expected = (region.width, region.height)
        else:
            image = ImageGrab.grab().convert("RGB")
            expected = None
            try:
                import pyautogui

                logical = pyautogui.size()
                expected = (logical.width, logical.height)
            except Exception:
                pass
    except Exception as exc:
        raise SystemExit(
            "screen capture failed; on macOS grant Screen Recording permission "
            "to the terminal or host app"
        ) from exc
    if expected is not None and image.size != tuple(expected):
        image = image.resize(tuple(expected), Image.LANCZOS)
    return image


def _capture_for_args(args) -> tuple[Image.Image, Region]:
    """Capture per CLI args; returns (image, region in global logical coords)."""
    display = getattr(args, "display", 1)
    image = _capture_image(args.region, display)
    if args.region is not None:
        local = args.region
    else:
        local = Region(x=0, y=0, width=image.width, height=image.height)
    bounds = _display_bounds()
    if bounds and 1 <= display <= len(bounds):
        dx, dy, _, _ = bounds[display - 1]
        return image, Region(
            x=dx + local.x, y=dy + local.y,
            width=local.width, height=local.height,
        )
    return image, local


def _mark_target(image: Image.Image, point: tuple[int, int]) -> Image.Image:
    """Draw a semi-transparent hit circle at ``point`` on a copy of ``image``."""
    marked = image.convert("RGBA")
    overlay = Image.new("RGBA", marked.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    x, y = point
    radius = max(12, min(image.size) // 60)
    draw.ellipse(
        (x - radius, y - radius, x + radius, y + radius),
        fill=(255, 0, 0, 70),
        outline=(255, 40, 40, 230),
        width=max(2, radius // 8),
    )
    draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=(255, 0, 0, 255))
    return Image.alpha_composite(marked, overlay).convert("RGB")


def _input_text(text: str, *, paste: bool = False, press_enter: int = 0) -> None:
    """Type or paste ``text`` at the current keyboard focus."""

    import pyautogui

    if paste:
        import pyperclip

        pyperclip.copy(text)
        if sys.platform == "darwin":
            pyautogui.keyDown("command")
            pyautogui.keyDown("v")
            pyautogui.keyUp("v")
            pyautogui.keyUp("command")
        else:
            pyautogui.keyDown("ctrl")
            pyautogui.keyDown("v")
            pyautogui.keyUp("v")
            pyautogui.keyUp("ctrl")
    else:
        pyautogui.typewrite(text, interval=0.01)
    for _ in range(press_enter):
        pyautogui.press("return")


def _resolve_text_target(args, target_text: str, *, prefix: str):
    """Locate ``target_text`` on screen and return the resolved click point + metadata.

    This shares the same OCR-first / vision-model-fallback logic as ``click``,
    but returns the data instead of clicking.
    """

    args.ocr_target = target_text
    if args.ocr_target and not getattr(args, "ocr_backend", None):
        args.ocr_backend = "auto"
    task = f"点击界面中的“{target_text}”"
    args.dir.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp()
    if args.image is not None:
        with Image.open(args.image) as source:
            clean = source.convert("RGB")
        clean_path = args.image
        region = args.region or Region(x=0, y=0, width=clean.width, height=clean.height)
    else:
        clean, region = _capture_for_args(args)
        clean_path = args.dir / f"{prefix}-{stamp}.clean.png"
        clean.save(clean_path)

    grid = _grid(args)
    gridded = render_numbered_grid(clean, grid)
    provider = _provider(args)
    try:
        action = provider.choose_action(
            task=task, clean=clean, gridded=gridded, grid=grid, step=1
        )
    except Exception as exc:
        return {
            "found": False,
            "error": f"{type(exc).__name__}: {exc}",
            "target": target_text,
            "task": task,
            "image": str(clean_path),
        }

    resolved = resolve_action(action, GridMapper(region, grid))
    metadata = getattr(provider, "last_metadata", None)
    method = "model"
    if metadata and metadata.get("ocr_direct"):
        method = "ocr"
    elif metadata and "ocr_backend" in metadata:
        method = "model+ocr-hints"

    if resolved is None:
        return {
            "found": False,
            "target": target_text,
            "task": task,
            "method": None,
            "image": str(clean_path),
            "model_action": action.model_dump(mode="json"),
            "metadata": metadata,
        }

    marked = _mark_target(
        clean, (resolved.start[0] - region.x, resolved.start[1] - region.y)
    )
    marked_path = args.dir / f"{prefix}-{stamp}.marked.png"
    marked.save(marked_path)
    return {
        "found": True,
        "x": resolved.start[0],
        "y": resolved.start[1],
        "method": method,
        "target": target_text,
        "task": task,
        "image": str(clean_path),
        "marked_image": str(marked_path),
        "model_action": action.model_dump(mode="json"),
        "zoom_trace": (
            provider.last_trace.as_dict()
            if getattr(provider, "last_trace", None) is not None
            else None
        ),
        "metadata": metadata,
        "_resolved": resolved,
        "_region": region,
    }


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "overlay":
        image = Image.open(args.image)
        grid = _grid(args)
        result = render_numbered_grid(image, grid)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result.save(args.output)
        if args.mapping:
            region = args.region or Region(
                x=0, y=0, width=image.width, height=image.height
            )
            mapper = GridMapper(region, grid)
            args.mapping.parent.mkdir(parents=True, exist_ok=True)
            args.mapping.write_text(
                json.dumps(mapper.mapping_table(), indent=2), encoding="utf-8"
            )
        print(args.output)
        return 0

    if args.command == "convert":
        action = parse_action(args.action)
        resolved = resolve_action(action, GridMapper(args.region, _grid(args)))
        print(json.dumps(None if resolved is None else resolved.__dict__, indent=2))
        return 0

    if args.command == "ocr":
        with Image.open(args.image) as source:
            clean = source.convert("RGB")
        options = {}
        if args.backend == "http":
            if not args.url:
                raise SystemExit("--backend http requires --url")
            options["endpoint"] = args.url
            if args.api_key_env:
                api_key = os.environ.get(args.api_key_env)
                if not api_key:
                    raise SystemExit(
                        f"environment variable {args.api_key_env!r} is empty"
                    )
                options["api_key"] = api_key
        backend = create_ocr_backend(
            args.backend, timeout=args.timeout, **options
        )
        result = backend.recognize(
            clean,
            OCRRequest(
                languages=tuple(args.languages or ("zh-Hans", "en-US")),
                mode=args.mode,
                custom_words=tuple(args.custom_word),
                min_confidence=args.min_confidence,
            ),
        )
        rendered = json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0

    if args.command == "ocr-serve":
        if not 1 <= args.port <= 65535:
            raise SystemExit("--port must be in 1..65535")
        api_key = None
        if args.api_key_env:
            api_key = os.environ.get(args.api_key_env)
            if not api_key:
                raise SystemExit(
                    f"environment variable {args.api_key_env!r} is empty"
                )
        backend = create_ocr_backend(args.backend, timeout=args.timeout)
        try:
            serve_ocr(backend, host=args.host, port=args.port, api_key=api_key)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        return 0

    if args.command == "shot":
        image, region = _capture_for_args(args)
        args.dir.mkdir(parents=True, exist_ok=True)
        path = args.dir / f"shot-{_timestamp()}.png"
        image.save(path)
        print(
            json.dumps(
                {
                    "path": str(path),
                    "width": image.width,
                    "height": image.height,
                    "display": args.display,
                    "region": region.model_dump(mode="json"),
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "gridshot":
        clean, region = _capture_for_args(args)
        grid = _grid(args)
        gridded = render_numbered_grid(clean, grid)
        args.dir.mkdir(parents=True, exist_ok=True)
        stamp = _timestamp()
        clean_path = args.dir / f"gridshot-{stamp}.clean.png"
        grid_path = args.dir / f"gridshot-{stamp}.grid.png"
        mapping_path = args.dir / f"gridshot-{stamp}.mapping.json"
        clean.save(clean_path)
        gridded.save(grid_path)
        mapper = GridMapper(region, grid)
        mapping = mapper.mapping_table()
        mapping_path.write_text(
            json.dumps(mapping, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "clean": str(clean_path),
                    "grid": str(grid_path),
                    "mapping": str(mapping_path),
                    "region": region.model_dump(mode="json"),
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "extendgrid":
        with Image.open(args.clean) as source:
            clean = source.convert("RGB")
        # The previous grid must cover the clean image exactly.
        prev_region = Region(x=0, y=0, width=clean.width, height=clean.height)
        prev_grid = GridSpec(rows=args.from_rows, cols=args.from_cols)
        prev_mapper = GridMapper(prev_region, prev_grid)
        bounds = prev_mapper.cell_bounds(args.cell, screen=True)
        local_bounds = prev_mapper.cell_bounds(args.cell, screen=False)
        crop = clean.crop((local_bounds.left, local_bounds.top, local_bounds.right, local_bounds.bottom))
        grid = _grid(args)
        gridded = render_numbered_grid(crop, grid)
        args.dir.mkdir(parents=True, exist_ok=True)
        stamp = _timestamp()
        clean_path = args.dir / f"extendgrid-{stamp}.clean.png"
        grid_path = args.dir / f"extendgrid-{stamp}.grid.png"
        mapping_path = args.dir / f"extendgrid-{stamp}.mapping.json"
        crop.save(clean_path)
        gridded.save(grid_path)
        sub_region = Region(
            x=bounds.left,
            y=bounds.top,
            width=bounds.width,
            height=bounds.height,
        )
        mapper = GridMapper(sub_region, grid)
        mapping = mapper.mapping_table()
        mapping_path.write_text(
            json.dumps(mapping, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "clean": str(clean_path),
                    "grid": str(grid_path),
                    "mapping": str(mapping_path),
                    "region": sub_region.model_dump(mode="json"),
                    "parent_cell": args.cell,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.command in ("find", "click"):
        if args.command == "click" and args.at is not None:
            x, y = args.at
            PyAutoGuiExecutor().execute(ResolvedAction(type="click", start=(x, y)))
            record = {"clicked": True, "x": x, "y": y}
            if not args.no_click_trace:
                args.dir.mkdir(parents=True, exist_ok=True)
                post, region = _capture_for_args(args)
                trace_path = args.dir / f"click-{_timestamp()}.png"
                marked = _mark_target(post, (x - region.x, y - region.y))
                marked.save(trace_path)
                record["click_trace"] = str(trace_path)
            print(json.dumps(record, ensure_ascii=False))
            return 0
        if not args.target and not args.task:
            raise SystemExit(f"vco {args.command} requires --target TEXT or --task TEXT")
        if args.provider == "none" and not args.target:
            raise SystemExit("--provider none locates via OCR only; pass --target TEXT")
        if args.target and not args.ocr_backend:
            args.ocr_backend = "auto"
        args.ocr_target = args.target
        task = args.task or f"点击界面中的“{args.target}”"

        args.dir.mkdir(parents=True, exist_ok=True)
        prefix = args.dir / f"{args.command}-{_timestamp()}"
        if args.image is not None:
            with Image.open(args.image) as source:
                clean = source.convert("RGB")
            clean_path = args.image
            region = args.region or Region(
                x=0, y=0, width=clean.width, height=clean.height
            )
        else:
            clean, region = _capture_for_args(args)
            clean_path = prefix.with_suffix(".clean.png")
            clean.save(clean_path)

        grid = _grid(args)
        gridded = render_numbered_grid(clean, grid)
        provider = _provider(args)
        try:
            action = provider.choose_action(
                task=task, clean=clean, gridded=gridded, grid=grid, step=1
            )
        except Exception as exc:
            record = {
                "found": False,
                "error": f"{type(exc).__name__}: {exc}",
                "target": args.target,
                "task": task,
                "image": str(clean_path),
            }
            print(json.dumps(record, indent=2, ensure_ascii=False))
            return 2

        resolved = resolve_action(action, GridMapper(region, grid))
        metadata = getattr(provider, "last_metadata", None)
        method = "model"
        if metadata and metadata.get("ocr_direct"):
            method = "ocr"
        elif metadata and "ocr_backend" in metadata:
            method = "model+ocr-hints"

        record = {
            "found": resolved is not None,
            "x": None if resolved is None else resolved.start[0],
            "y": None if resolved is None else resolved.start[1],
            "method": method if resolved is not None else None,
            "target": args.target,
            "task": task,
            "image": str(clean_path),
            "marked_image": None,
            "model_action": action.model_dump(mode="json"),
            "zoom_trace": (
                provider.last_trace.as_dict()
                if getattr(provider, "last_trace", None) is not None
                else None
            ),
            "metadata": metadata,
            "executed": False,
        }
        if resolved is not None:
            marked = _mark_target(
                clean, (resolved.start[0] - region.x, resolved.start[1] - region.y)
            )
            marked_path = prefix.with_suffix(".marked.png")
            marked.save(marked_path)
            record["marked_image"] = str(marked_path)
        json_path = prefix.with_suffix(".json")
        if (
            args.command == "click"
            and resolved is not None
            and resolved.type == "click"
        ):
            if not args.no_verify and args.image is None:
                fresh, fresh_region = _capture_for_args(args)
                stable, similarity = verify_target_stable(
                    clean,
                    fresh,
                    resolved.start,
                    threshold=args.verify_threshold,
                )
                record["verified"] = stable
                record["similarity"] = round(similarity, 4)
                if not stable:
                    diff_img = make_diff_image(
                        clean, fresh, resolved.start
                    )
                    diff_path = args.dir / f"diff-{_timestamp()}.png"
                    diff_img.save(diff_path)
                    record["diff_image"] = str(diff_path)
                    record["error"] = (
                        f"target region changed before click "
                        f"(similarity {similarity:.2%} < {args.verify_threshold:.0%})"
                    )
                    json_path.write_text(
                        json.dumps(record, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    record["record"] = str(json_path)
                    print(json.dumps(record, indent=2, ensure_ascii=False))
                    return 2
            PyAutoGuiExecutor().execute(
                ResolvedAction(type="click", start=resolved.start)
            )
            record["executed"] = True
            if not args.no_click_trace:
                post, region = _capture_for_args(args)
                trace_path = args.dir / f"click-{_timestamp()}.png"
                marked = _mark_target(
                    post,
                    (
                        resolved.start[0] - region.x,
                        resolved.start[1] - region.y,
                    ),
                )
                marked.save(trace_path)
                record["click_trace"] = str(trace_path)
        json_path.write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        record["record"] = str(json_path)
        print(json.dumps(record, indent=2, ensure_ascii=False))
        return 0 if resolved is not None else 2

    if args.command == "type":
        text = args.text
        if args.text_env:
            text = os.environ.get(args.text_env)
            if text is None:
                raise SystemExit(f"environment variable {args.text_env!r} is not set")
        if text is None:
            raise SystemExit("vco type requires --text or --text-env")
        if args.at is None and args.target is None:
            raise SystemExit("vco type requires --target TEXT or --at x,y")

        if args.at is not None:
            PyAutoGuiExecutor().execute(ResolvedAction(type="click", start=args.at))
            result = {"found": True, "x": args.at[0], "y": args.at[1], "method": "direct"}
        else:
            result = _resolve_text_target(args, args.target, prefix="type")
            if not result.get("found"):
                print(json.dumps(result, indent=2, ensure_ascii=False))
                return 2
            resolved = result.pop("_resolved")
            result.pop("_region", None)
            PyAutoGuiExecutor().execute(ResolvedAction(type="click", start=resolved.start))
        if args.settle:
            time.sleep(args.settle)
        _input_text(text, paste=args.paste, press_enter=getattr(args, "press_enter", 0))
        result.update({"text_entered": True, "paste": args.paste})
        json_path = args.dir / f"type-{_timestamp()}.json"
        json_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        result["record"] = str(json_path)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "fill":
        fields = []
        for raw in args.field:
            if "=" not in raw:
                raise SystemExit(f"--field must be label=text, got {raw!r}")
            label, value = raw.split("=", 1)
            fields.append((label, value))

        filled = []
        for label, value in fields:
            result = _resolve_text_target(args, label, prefix="fill")
            if not result.get("found"):
                print(json.dumps({"filled": filled, "failed": result}, indent=2, ensure_ascii=False))
                return 2
            resolved = result.pop("_resolved")
            result.pop("_region", None)
            PyAutoGuiExecutor().execute(ResolvedAction(type="click", start=resolved.start))
            if args.settle:
                time.sleep(args.settle)
            _input_text(value, paste=args.paste, press_enter=getattr(args, "press_enter", 0))
            filled.append({"label": label, "value": value, "point": resolved.start})

        submit_result = None
        if args.target:
            submit_result = _resolve_text_target(args, args.target, prefix="submit")
            if submit_result.get("found"):
                resolved = submit_result.pop("_resolved")
                submit_result.pop("_region", None)
                PyAutoGuiExecutor().execute(ResolvedAction(type="click", start=resolved.start))

        record = {"filled": filled, "submitted": args.target is not None, "submit": submit_result}
        json_path = args.dir / f"fill-{_timestamp()}.json"
        json_path.write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        record["record"] = str(json_path)
        print(json.dumps(record, indent=2, ensure_ascii=False))
        return 0

    if args.command == "ask":
        if args.image is not None:
            with Image.open(args.image) as source:
                image = source.convert("RGB")
            image_path = str(args.image)
        else:
            image, _ = _capture_for_args(args)
            args.dir.mkdir(parents=True, exist_ok=True)
            shot_path = args.dir / f"shot-{_timestamp()}.png"
            image.save(shot_path)
            image_path = str(shot_path)
        provider = _base_provider(args)
        try:
            answer = provider.ask(question=args.question, image=image)
        except (RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        record = {
            "answer": answer,
            "question": args.question,
            "image": image_path,
            "provider": args.provider,
        }
        record.update(getattr(provider, "last_metadata", None) or {})
        print(json.dumps(record, indent=2, ensure_ascii=False))
        return 0

    if args.command == "webshot":
        stamp = _timestamp()
        if args.output is None:
            args.dir.mkdir(parents=True, exist_ok=True)
            args.output = args.dir / f"webshot-{stamp}.png"
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
        from .browser import screenshot as web_screenshot

        try:
            result = web_screenshot(
                args.url,
                str(args.output),
                full_page=args.full_page,
                width=args.width,
                height=args.height,
                timeout=args.timeout,
                settle=args.settle,
                profile=None if args.profile is None else str(args.profile),
                headless=not args.headed,
                hold=args.hold,
                record=(
                    str(args.dir / f"webshot-{stamp}.webm") if args.record else None
                ),
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.command == "webclick":
        if not args.target and not args.selector:
            raise SystemExit("webclick requires --target TEXT or --selector SELECTOR")
        args.dir.mkdir(parents=True, exist_ok=True)
        stamp = _timestamp()
        from .browser import click as web_click

        try:
            result = web_click(
                args.url,
                args.target,
                selector=args.selector,
                contains=args.contains,
                fills=args.fill,
                width=args.width,
                height=args.height,
                timeout=args.timeout,
                settle=args.settle,
                profile=None if args.profile is None else str(args.profile),
                headless=not args.headed,
                hold=args.hold,
                expect=args.expect,
                expect_timeout=args.expect_timeout,
                record=(
                    str(args.dir / f"webclick-{stamp}.webm") if args.record else None
                ),
                before_path=str(args.dir / f"webclick-{stamp}.before.png"),
                after_path=str(args.dir / f"webclick-{stamp}.after.png"),
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["clicked"] else 2

    if args.command == "webtext":
        from .browser import snapshot as web_snapshot

        try:
            result = web_snapshot(
                args.url,
                width=args.width,
                height=args.height,
                timeout=args.timeout,
                settle=args.settle,
                profile=None if args.profile is None else str(args.profile),
                headless=not args.headed,
                hold=args.hold,
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "webdebug":
        if args.max_steps < 1:
            raise SystemExit("--max-steps must be at least 1")
        from .webdebug import debug as web_debug

        provider = _base_provider(args)
        if not hasattr(provider, "chat"):
            raise SystemExit(f"provider {args.provider} does not support text chat")
        findings = web_debug(
            args.task,
            args.url,
            provider,
            max_steps=args.max_steps,
            settle=args.settle,
            width=args.width,
            height=args.height,
            timeout=args.timeout,
            profile=None if args.profile is None else str(args.profile),
            headless=not args.headed,
            hold=args.hold,
            record=args.record,
            artifact_dir=args.dir,
        )
        print(json.dumps(findings, indent=2, ensure_ascii=False))
        # Reproducing the bug is the successful outcome here; failing to is
        # the one worth a non-zero code, because it means nothing was learned.
        return 0 if findings["reproduced"] else 2

    if args.command == "webrun":
        if args.max_steps < 1:
            raise SystemExit("--max-steps must be at least 1")
        from .webagent import run as web_run

        provider = _base_provider(args)
        if not hasattr(provider, "chat"):
            raise SystemExit(f"provider {args.provider} does not support text chat")
        result = web_run(
            args.task,
            args.url,
            provider,
            max_steps=args.max_steps,
            settle=args.settle,
            width=args.width,
            height=args.height,
            timeout=args.timeout,
            profile=None if args.profile is None else str(args.profile),
            headless=not args.headed,
            hold=args.hold,
            record=args.record,
            artifact_dir=args.dir,
        )
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
        return 0 if result.status == "done" else 2

    if args.command == "probe":
        with Image.open(args.image) as source:
            clean = source.convert("RGB")
        grid = _grid(args)
        gridded = render_numbered_grid(clean, grid)
        if args.output_grid:
            args.output_grid.parent.mkdir(parents=True, exist_ok=True)
            gridded.save(args.output_grid)
        provider = _provider(args)
        action = provider.choose_action(
            task=args.task,
            clean=clean,
            gridded=gridded,
            grid=grid,
            step=1,
        )
        region = args.region or Region(x=0, y=0, width=clean.width, height=clean.height)
        resolved = resolve_action(action, GridMapper(region, grid))
        zoom_trace = None
        if getattr(provider, "last_trace", None) is not None:
            zoom_trace = provider.last_trace.as_dict()
            if (
                args.output_grid
                and isinstance(provider, ZoomProvider)
                and provider.last_zoom_grid is not None
            ):
                suffix = args.output_grid.suffix or ".png"
                zoom_clean_path = args.output_grid.with_name(
                    f"{args.output_grid.stem}-zoom-clean{suffix}"
                )
                zoom_grid_path = args.output_grid.with_name(
                    f"{args.output_grid.stem}-zoom-grid{suffix}"
                )
                provider.last_zoom_clean.save(zoom_clean_path)
                provider.last_zoom_grid.save(zoom_grid_path)
            elif args.output_grid:
                suffix = args.output_grid.suffix or ".png"
                for level, (zoom_clean, zoom_grid) in enumerate(
                    getattr(provider, "last_zoom_images", []), start=2
                ):
                    zoom_clean.save(
                        args.output_grid.with_name(
                            f"{args.output_grid.stem}-zoom-{level:02d}-clean{suffix}"
                        )
                    )
                    zoom_grid.save(
                        args.output_grid.with_name(
                            f"{args.output_grid.stem}-zoom-{level:02d}-grid{suffix}"
                        )
                    )
        record = {
            "model_action": action.model_dump(mode="json"),
            "resolved_local_action": (
                None if resolved is None else resolved.__dict__
            ),
            "zoom_trace": zoom_trace,
            "provider_metadata": getattr(provider, "last_metadata", None),
        }
        rendered = json.dumps(record, indent=2, ensure_ascii=False)
        if args.output_json:
            args.output_json.parent.mkdir(parents=True, exist_ok=True)
            args.output_json.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0

    if args.max_steps < 1:
        raise SystemExit("--max-steps must be at least 1")
    if args.settle < 0:
        raise SystemExit("--settle cannot be negative")
    executor = PyAutoGuiExecutor() if args.execute else DryRunExecutor()
    loop = ComputerUseLoop(
        capture=PillowScreenCapture(),
        provider=_provider(args),
        executor=executor,
        region=args.region,
        grid=_grid(args),
        artifact_root=args.artifacts,
        settle_seconds=args.settle,
    )
    result = loop.run(args.task, max_steps=args.max_steps)
    print(
        json.dumps(
            {
                "status": result.status,
                "steps": result.steps,
                "reason": result.reason,
                "run_dir": str(result.run_dir),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if result.status == "done" else 2


if __name__ == "__main__":
    raise SystemExit(main())
