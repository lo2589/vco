---
name: operate-screen
description: Locate, click, and repeatedly operate visible controls inside an explicitly bounded screen region using GLM-4.6V or MiniMax-M3, a numbered 16x16 grid, and adaptive zoom. Use when the user asks Codex to inspect a screen target, test whether a control can be clicked, perform bounded mouse-only UI steps, run a visible click loop, inspect VCO run artifacts, or clear the isolated VCO cache.
---

# Operate Screen

Use the `visual-computer-operate` MCP tools. Keep every capture and action inside the user-provided region.

## Workflow

1. Require `region={x,y,width,height}`. If it is unknown and cannot be inferred safely, ask the user for it.
2. Call `screen_probe` first for a new region, changed layout, or uncertain target. Use `provider="glm"`, `image_mode="both"`, and `thinking="disabled"` unless the user requests MiniMax. This never moves the mouse.
3. Report the proposed action, resolved coordinate, adaptive-zoom depth, latency, and cache path.
4. Call `screen_run` with `execute=true` only when the user explicitly asked to perform the screen action. Use a small `max_steps` first; increase it only when the visible task requires more iterations.
5. Stop when the tool reports `done`, reaches `max_steps`, or returns an error. Do not invent a successful click when the tool stopped.

Use `zoom_strategy="model"` for large, clear controls. Use `zoom_strategy="fixed"` with three levels for menu-bar icons, tiny targets, visually similar labels, or after a model-directed crop misses. Fixed adaptive zoom requires a coordinate at every level, refines the selected neighborhood, and forces the final level to click. Do not use the legacy 4x4 `--zoom` protocol.

## Safety

- Prefer `screen_probe` when the user asks only to locate, inspect, test, or diagnose.
- Treat `screen_run execute=true` as an external side effect. Do not use it without clear action intent.
- Do not use this skill for payments, purchases, account changes, deletion, sending messages, or other high-impact actions unless the user explicitly authorizes that exact action and target.
- Keep `max_steps` bounded. Never broaden the region to the whole desktop without explicit instruction.
- The tool supports mouse clicks and straight drags; it does not provide keyboard entry or shell access.

## Cache

Use `cache_status` for a read-only summary. Before calling `cache_clear`, ask for confirmation unless the user already explicitly requested cache deletion in the current turn; then pass `confirm=true`. The cache tool is restricted to this project's `cache/` child directory.

## Web tools (no vision model needed)

For web pages, prefer the headless DOM tools over screen tools — they are deterministic and never touch the real mouse:

- `web_snapshot`: return the page accessibility tree (roles + text) plus console/page/request errors. Use it to understand a page, including with text-only models.
- `web_screenshot`: render and save a screenshot; `record=true` also saves a .webm video.
- `web_click`: fill inputs (`fills=["placeholder=text"]`) and click a unique `target` text or `selector`; refuses ambiguous targets; `expect` verifies post-click page text; `record=true` saves a video.
- `web_run`: text-model agent loop (aria snapshot → model action → DOM execute) until done or `max_steps`; `provider` defaults to `ollama` (requires `model`), `glm`/`minimax` use the configured settings.
