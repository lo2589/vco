"""Zero-dependency stdio MCP server for Visual Computer Operate."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .adaptive_zoom import AdaptiveZoomProvider
from .capture import PillowScreenCapture
from .executor import DryRunExecutor, PyAutoGuiExecutor, resolve_action
from .geometry import GridMapper
from .grid import render_numbered_grid
from .loop import ComputerUseLoop
from .models import GridSpec, Region
from .providers import GLMProvider, MiniMaxProvider


PROJECT_ROOT = Path(__file__).resolve().parents[1]


TOOLS = [
    {
        "name": "screen_probe",
        "description": (
            "Capture a bounded screen region and ask GLM-4.6V or MiniMax-M3 to locate the next "
            "control. Never moves the mouse."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "minLength": 1},
                "region": {"$ref": "#/$defs/region"},
                "provider": {
                    "type": "string",
                    "enum": ["glm", "minimax"],
                    "default": "glm",
                },
                "image_mode": {
                    "type": "string",
                    "enum": ["both", "clean", "grid"],
                    "default": "both",
                },
                "thinking": {
                    "type": "string",
                    "enum": ["disabled", "enabled"],
                    "default": "disabled",
                },
                "image_detail": {
                    "type": "string",
                    "enum": ["low", "default", "high"],
                    "default": "default",
                },
                "service_tier": {
                    "type": "string",
                    "enum": ["standard", "priority"],
                    "default": "priority",
                },
                "max_zoom_levels": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 6,
                    "default": 3,
                },
                "zoom_strategy": {
                    "type": "string",
                    "enum": ["model", "fixed"],
                    "default": "model",
                },
            },
            "required": ["task", "region"],
            "additionalProperties": False,
            "$defs": {
                "region": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 16},
                        "height": {"type": "integer", "minimum": 16},
                    },
                    "required": ["x", "y", "width", "height"],
                    "additionalProperties": False,
                }
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "screen_run",
        "description": (
            "Run the screenshot-grid-action loop inside a bounded screen region. "
            "Mouse input occurs only when execute=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "minLength": 1},
                "region": {"$ref": "#/$defs/region"},
                "provider": {
                    "type": "string",
                    "enum": ["glm", "minimax"],
                    "default": "glm",
                },
                "image_mode": {
                    "type": "string",
                    "enum": ["both", "clean", "grid"],
                    "default": "both",
                },
                "thinking": {
                    "type": "string",
                    "enum": ["disabled", "enabled"],
                    "default": "disabled",
                },
                "execute": {"type": "boolean", "default": False},
                "max_steps": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 12,
                },
                "settle_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 30,
                    "default": 1,
                },
                "max_zoom_levels": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 6,
                    "default": 3,
                },
                "zoom_strategy": {
                    "type": "string",
                    "enum": ["model", "fixed"],
                    "default": "model",
                },
            },
            "required": ["task", "region", "execute"],
            "additionalProperties": False,
            "$defs": {
                "region": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 16},
                        "height": {"type": "integer", "minimum": 16},
                    },
                    "required": ["x", "y", "width", "height"],
                    "additionalProperties": False,
                }
            },
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "web_snapshot",
        "description": (
            "Open a URL in headless Chromium and return the page's accessibility "
            "tree as text plus console/page/request errors. No vision model needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 1},
                "settle": {"type": "number", "minimum": 0, "maximum": 30, "default": 0},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "web_screenshot",
        "description": "Render a URL in headless Chromium and save a screenshot to the cache.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 1},
                "full_page": {"type": "boolean", "default": False},
                "width": {"type": "integer", "default": 1280},
                "height": {"type": "integer", "default": 800},
                "settle": {"type": "number", "minimum": 0, "maximum": 30, "default": 0},
                "record": {"type": "boolean", "default": False},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "web_click",
        "description": (
            "Open a URL in headless Chromium, optionally fill inputs "
            "(placeholder=text pairs), then DOM-click a unique text target. "
            "Never touches the real mouse. Refuses ambiguous targets."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 1},
                "target": {"type": "string"},
                "selector": {"type": "string"},
                "contains": {"type": "boolean", "default": False},
                "fills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
                "expect": {"type": "string"},
                "settle": {"type": "number", "minimum": 0, "maximum": 30, "default": 0.5},
                "record": {"type": "boolean", "default": False},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "web_run",
        "description": (
            "Agent loop: a text model reads the accessibility tree and drives the "
            "page (click/fill/done) until the task completes or max_steps."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 1},
                "task": {"type": "string", "minLength": 1},
                "provider": {
                    "type": "string",
                    "enum": ["ollama", "glm", "minimax"],
                    "default": "ollama",
                },
                "model": {"type": "string"},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8},
                "record": {"type": "boolean", "default": False},
            },
            "required": ["url", "task"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "cache_status",
        "description": "Show the size and latest entries in the isolated VCO cache.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "cache_clear",
        "description": "Delete only the isolated cache directory for this VCO project.",
        "inputSchema": {
            "type": "object",
            "properties": {"confirm": {"type": "boolean"}},
            "required": ["confirm"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
]


def _cache_root() -> Path:
    configured = os.environ.get("VCO_CACHE_ROOT")
    root = Path(configured).expanduser() if configured else PROJECT_ROOT / "cache"
    root = root.resolve()
    project = PROJECT_ROOT.resolve()
    if root == project or project not in root.parents:
        raise RuntimeError("VCO_CACHE_ROOT must be a child of the project directory")
    return root


def _settings(env_name: str, label: str) -> dict:
    path = os.environ.get(env_name)
    if not path:
        raise RuntimeError(f"{env_name} is not configured")
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    key = payload.get("api_key")
    if not isinstance(key, str) or not key.strip():
        raise RuntimeError(f"{label} settings contain no api_key")
    return payload


def _region(value) -> Region:
    if not isinstance(value, dict):
        raise ValueError("region must be an object")
    return Region.model_validate(value)


def _provider(arguments: dict, *, force_initial_click: bool):
    provider_name = arguments.get("provider", "glm")
    if provider_name == "glm":
        settings = _settings("VCO_GLM_SETTINGS", "GLM")
        base = GLMProvider(
            "glm-4.6v",
            api_key=settings["api_key"],
            base_url=settings.get(
                "base_url", "https://open.bigmodel.cn/api/paas/v4/"
            ),
            timeout=120,
            image_mode=arguments.get("image_mode", "both"),
            thinking=arguments.get("thinking", "disabled"),
        )
    elif provider_name == "minimax":
        settings = _settings("VCO_MINIMAX_SETTINGS", "MiniMax")
        base = MiniMaxProvider(
            "MiniMax-M3",
            api_key=settings["api_key"],
            timeout=60,
            service_tier=arguments.get("service_tier", "priority"),
            image_detail=arguments.get("image_detail", "default"),
            image_mode=arguments.get("image_mode", "both"),
        )
    else:
        raise ValueError("provider must be glm or minimax")
    return AdaptiveZoomProvider(
        base,
        max_levels=int(arguments.get("max_zoom_levels", 3)),
        span_cells=4,
        force_initial_click=force_initial_click,
        always_refine=arguments.get("zoom_strategy", "model") == "fixed",
    )


def _stamp_dir(parent: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    result = _cache_root() / parent / stamp
    result.mkdir(parents=True, exist_ok=False)
    return result


def _save_probe_artifacts(run_dir, clean, gridded, provider, record):
    clean.save(run_dir / "clean.png")
    gridded.save(run_dir / "grid.png")
    for level, (zoom_clean, zoom_grid) in enumerate(
        provider.last_zoom_images, start=2
    ):
        zoom_clean.save(run_dir / f"zoom-{level:02d}-clean.png")
        zoom_grid.save(run_dir / f"zoom-{level:02d}-grid.png")
    (run_dir / "result.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def screen_probe(arguments: dict) -> dict:
    task = str(arguments["task"])
    region = _region(arguments["region"])
    grid = GridSpec(rows=16, cols=16)
    provider = _provider(arguments, force_initial_click=True)
    frame = PillowScreenCapture().capture(region)
    gridded = render_numbered_grid(frame.image, grid)
    action = provider.choose_action(
        task=task,
        clean=frame.image,
        gridded=gridded,
        grid=grid,
        step=1,
    )
    resolved = resolve_action(action, GridMapper(region, grid))
    run_dir = _stamp_dir("mcp-probes")
    record = {
        "task": task,
        "region": region.model_dump(mode="json"),
        "model_action": action.model_dump(mode="json"),
        "resolved_action": None if resolved is None else resolved.__dict__,
        "adaptive_zoom": provider.last_trace.as_dict(),
        "provider_metadata": provider.last_metadata,
        "executed": False,
        "cache_dir": str(run_dir),
    }
    _save_probe_artifacts(run_dir, frame.image, gridded, provider, record)
    return record


def screen_run(arguments: dict) -> dict:
    task = str(arguments["task"])
    region = _region(arguments["region"])
    execute = bool(arguments["execute"])
    max_steps = int(arguments.get("max_steps", 12))
    if not execute:
        max_steps = 1
    provider = _provider(arguments, force_initial_click=False)
    executor = PyAutoGuiExecutor() if execute else DryRunExecutor()
    artifact_root = _cache_root() / "mcp-runs"
    loop = ComputerUseLoop(
        capture=PillowScreenCapture(),
        provider=provider,
        executor=executor,
        region=region,
        grid=GridSpec(rows=16, cols=16),
        artifact_root=artifact_root,
        settle_seconds=float(arguments.get("settle_seconds", 1)),
    )
    with contextlib.redirect_stdout(io.StringIO()):
        result = loop.run(task, max_steps=max_steps)
    return {
        "status": result.status,
        "steps": result.steps,
        "reason": result.reason,
        "executed": execute,
        "cache_dir": str(result.run_dir),
    }


def cache_status(_arguments: dict) -> dict:
    root = _cache_root()
    files = list(root.rglob("*")) if root.exists() else []
    regular = [path for path in files if path.is_file()]
    latest = sorted(regular, key=lambda path: path.stat().st_mtime, reverse=True)[:10]
    return {
        "cache_root": str(root),
        "file_count": len(regular),
        "size_bytes": sum(path.stat().st_size for path in regular),
        "latest_files": [str(path.relative_to(root)) for path in latest],
    }


def cache_clear(arguments: dict) -> dict:
    if arguments.get("confirm") is not True:
        raise ValueError("cache_clear requires confirm=true")
    root = _cache_root()
    before = cache_status({})
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    return {"cleared": True, "cache_root": str(root), "previous": before}


def web_snapshot(arguments: dict) -> dict:
    from .browser import snapshot

    return snapshot(
        str(arguments["url"]),
        settle=float(arguments.get("settle", 0)),
    )


def web_screenshot(arguments: dict) -> dict:
    from .browser import screenshot

    run_dir = _stamp_dir("mcp-webshots")
    record = arguments.get("record", False)
    return screenshot(
        str(arguments["url"]),
        str(run_dir / "page.png"),
        full_page=bool(arguments.get("full_page", False)),
        width=int(arguments.get("width", 1280)),
        height=int(arguments.get("height", 800)),
        settle=float(arguments.get("settle", 0)),
        record=str(run_dir / "session.webm") if record else None,
    )


def web_click(arguments: dict) -> dict:
    from .browser import click

    target = arguments.get("target")
    selector = arguments.get("selector")
    if not target and not selector:
        raise ValueError("web_click requires target or selector")
    run_dir = _stamp_dir("mcp-webclicks")
    record = arguments.get("record", False)
    result = click(
        str(arguments["url"]),
        target,
        selector=selector,
        contains=bool(arguments.get("contains", False)),
        fills=[str(item) for item in arguments.get("fills", [])],
        expect=arguments.get("expect"),
        settle=float(arguments.get("settle", 0.5)),
        record=str(run_dir / "session.webm") if record else None,
        before_path=str(run_dir / "before.png"),
        after_path=str(run_dir / "after.png"),
    )
    result["cache_dir"] = str(run_dir)
    return result


def _chat_provider(arguments: dict):
    from .providers import OllamaProvider

    provider_name = arguments.get("provider", "ollama")
    if provider_name == "ollama":
        model = arguments.get("model")
        if not model:
            raise ValueError("web_run with provider=ollama requires model")
        return OllamaProvider(model)
    if provider_name == "glm":
        settings = _settings("VCO_GLM_SETTINGS", "GLM")
        return GLMProvider(
            "glm-4.6v",
            api_key=settings["api_key"],
            base_url=settings.get("base_url", "https://open.bigmodel.cn/api/paas/v4/"),
            timeout=120,
        )
    if provider_name == "minimax":
        settings = _settings("VCO_MINIMAX_SETTINGS", "MiniMax")
        return MiniMaxProvider(
            "MiniMax-M3", api_key=settings["api_key"], timeout=60
        )
    raise ValueError("provider must be ollama, glm, or minimax")


def web_run(arguments: dict) -> dict:
    from .webagent import run

    run_dir = _stamp_dir("mcp-webruns")
    result = run(
        str(arguments["task"]),
        str(arguments["url"]),
        _chat_provider(arguments),
        max_steps=int(arguments.get("max_steps", 8)),
        record=bool(arguments.get("record", False)),
        artifact_dir=run_dir,
    )
    output = result.as_dict()
    output["cache_dir"] = str(run_dir)
    return output


HANDLERS = {
    "screen_probe": screen_probe,
    "screen_run": screen_run,
    "web_snapshot": web_snapshot,
    "web_screenshot": web_screenshot,
    "web_click": web_click,
    "web_run": web_run,
    "cache_status": cache_status,
    "cache_clear": cache_clear,
}


def _tool_result(value: dict, *, is_error: bool = False) -> dict:
    return {
        "content": [
            {"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}
        ],
        "structuredContent": value,
        "isError": is_error,
    }


def _dispatch(message: dict):
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        protocol = (message.get("params") or {}).get(
            "protocolVersion", "2024-11-05"
        )
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "visual-computer-operate", "version": "0.1.0"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            handler = HANDLERS[name]
            result = _tool_result(handler(arguments))
        except Exception as exc:
            result = _tool_result(
                {"error": f"{type(exc).__name__}: {exc}"}, is_error=True
            )
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> int:
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            response = _dispatch(message)
            if response is not None:
                sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            error = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": f"Internal error: {exc}"},
            }
            sys.stdout.write(json.dumps(error, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
