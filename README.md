# vco — visual computer operate for LLMs

[![PyPI](https://img.shields.io/pypi/v/vco.svg)](https://pypi.org/project/vco/)
[中文文档](README_zh.md)

`vco` is a clicker for LLMs: it lets any agent that can run shell commands and read JSON operate web pages and desktop screens.

Two channels, deterministic-first, vision models only as fallback:

- **Web**: headless Chromium over the DOM — accessibility-tree perception (readable by text-only models), click by text/selector, form filling, error capture, video recording. No vision model involved at all.
- **Desktop**: screenshot + OCR locating (a unique text hit needs zero model calls); only text-free targets fall back to a vision model picking cells on a numbered grid, while local code handles coordinate math and the real mouse.

![vco architecture](assets/vco-architecture.png)

## Commands

stdout is always JSON. Artifacts go to `.screenshot/` under the **current working directory** (whichever project you run it from).

Web (headless, never touches the real mouse):

```bash
vco webshot https://example.com   # render a page and screenshot it; --full-page for the whole page
                                  # output includes console_errors/page_errors/failed_requests
vco webtext https://example.com   # dump the accessibility tree (roles + text as YAML);
                                  # this is how a text-only model "sees" a page
vco webclick http://127.0.0.1:9005 --fill 输入消息=你好 --target 发送
                                  # fill inputs (repeatable --fill) then click; ambiguous targets are
                                  # refused and listed; before/after screenshots are saved
                                  # --expect "text" waits for post-click content (verified field)
                                  # --profile <dir> persists login/cookies; --selector clicks any CSS target
                                  # --headed --hold 3 shows a live demo (visible typing, orange halo on target)
                                  # --record saves a .webm video of the whole session (works headless)
vco webrun http://127.0.0.1:9005 --task 'type xxx and click send' --provider ollama --model qwen3:8b
                                  # text-model agent loop: snapshot → decision → DOM action → repeat until done
vco webdebug http://127.0.0.1:9005 --task 'I scrolled up to read, sent a message, and it jumped to the top'
                                  # front-end debugger: reproduces the complaint, records why, reports; never edits
                                  # findings are built from what was recorded, so an unproven cause cannot appear
```

Desktop (OCR first; the real mouse moves only via `click`):

```bash
vco shot                          # screenshot; --region x,y,w,h for a sub-region;
                                  # --display 2 selects a secondary display (default: main);
                                  # coordinates are always global logical pixels, ready for click
vco find --target "CODEX"         # locate (dry-run): OCR first, draws a translucent red circle on hit
vco click --at 1164,92            # real mouse click; click --target "..." locates first
vco type --target "Username" --text alice
                                  # locate a text field and type into it; --paste uses the clipboard
vco type --target "Password" --text-env PASSWORD
                                  # read the secret from an env var so it never enters shell history
vco fill --field "Username=alice" --field "Password=secret" --target "Sign in"
                                  # fill multiple fields, then click the submit button; use --paste for CJK
vco ask [image] --provider ollama --model minicpm-v4.6:latest
                                  # ask a vision model "what's in this image"; captures the screen if no image
```

Recommended desktop workflow (every step verifiable):

```text
shot            see the current state
find --target   locate + circle confirmation image (marked_image); never moves the mouse
  │             ├─ unique OCR hit → coordinates computed locally, zero model calls
  │             ├─ multiple hits  → refuses to click; refine the target or add --provider
  │             └─ zero hits      → add --provider for vision-model grid/zoom locating
click --at      real click after the circle looks right
shot            screenshot again to verify the UI actually changed
```

Web is simpler: `webtext` to perceive → `webclick` to act (`--expect` verifies inline); or hand the whole task to `webrun` and let a text model drive. For a bug rather than a task, `webdebug` — see below.

Key `find`/`click` JSON fields: `found` (exit code 2 when false — a normal outcome, not a failure), `x`/`y`, `method` (`ocr` / `model` / `model+ocr-hints`), `marked_image`, `metadata.elapsed_seconds`.

`ask` supports four providers: `ollama` (local, default `127.0.0.1:11434`), `minimax` and `glm` (need `--minimax-settings` / `--glm-settings` pointing at a JSON file with an api_key), and `openai`. Use `--question` to customize.

Full usage instructions (including safety rules and failure handling) are packaged as a tool-agnostic agent skill: [`skills/operate-screen/SKILL.md`](skills/operate-screen/SKILL.md) — drop it into any agent's skills directory. This repo's `.kimi-code/skills/operate-screen` is a symlink to it, and `plugins/vco/` is the Codex MCP packaging.

## Why vco over other Computer-Use tools?

There are three other approaches people often ask about. They are good at different things; `vco` exists because none of them is the cheapest, most controllable fit for **text-heavy web pages and desktop UIs driven by an arbitrary LLM**.

| | OmniParser | UI-TARS | Qwen-Omni | vco |
|---|---|---|---|---|
| What it is | Pure-vision UI parser from a screenshot | End-to-end GUI-Agent model + runtime | General multimodal model (vision + audio + text) | Composable toolchain: OCR, DOM/aria, optional vision model, real mouse/keyboard |
| Model lock-in | Owns a vision model | Tied to UI-TARS weights | Tied to Qwen family | **Model-agnostic** — any LLM that can call shell/JSON |
| Perception | Screenshot only | Screenshot only | Screenshot / uploaded image | **OCR-first for text**, DOM/aria for web, vision only as fallback |
| Control | Perception only | Action plans executed by framework | Needs external control code | **Built-in control**: `click`, `type`, `webclick`, `webrun` |
| Cost / latency | Vision model per screenshot | Large model per step | Large multimodal call per step | **Zero model calls** for unique text targets; local OCR + local coordinate math |
| Best for | Complex desktop apps with no API | Zero-code desktop automation | Multimodal tasks inside the Qwen ecosystem | Web automation, text-heavy desktop UIs, agent toolchains, local/offline setups |

### OmniParser

OmniParser turns a screenshot into a list of clickable elements. It is useful when the target has **no text and no API** — bare icons, custom-drawn controls, game-like UIs. For ordinary apps and web pages it is usually overkill: it cannot read the DOM, misses disabled-state information, and runs a vision model on every frame. `vco` uses OCR for text targets and only pays for a vision model when OCR fails.

### UI-TARS

UI-TARS is closer to a complete zero-code agent: give it a goal and it plans and executes desktop actions. That is powerful for complex one-shot desktop tasks, but it couples you to the UI-TARS model and runtime. `vco` is the opposite: small, composable shell primitives that any agent framework can orchestrate, swap models with, and audit step-by-step.

### Qwen-Omni

Qwen-Omni is a general multimodal model. It can look at a screenshot and suggest an action, but it does not ship with screen control, coordinate math, or retry logic. You still need a surrounding tool to take the screenshot, move the mouse, and handle misses. `vco` is that surrounding tool, while staying free to use Qwen, GPT-4o, Claude, or a local model as the brain.

For web automation, form filling, verifying rendered output, local Computer-Use proofs of concept, or wiring perception/control into your own agent — `vco` is smaller, cheaper, and model-agnostic.

## Debugging a front-end bug

`webdebug` takes a complaint in the words someone actually used and comes back with the line that caused it. It reproduces and reports; it never edits. Fixing needs the whole codebase in your head and is where the risk lives, while locating is where the hours go — by the time you know which line moved the value, the edit is usually obvious.

```bash
vco webdebug http://127.0.0.1:8772/ \
  --task "I scrolled down to read something, hit Refresh, and the list jumped back to the top" \
  --provider ollama --model qwen3.6:latest
```

```json
{
  "reproduced": true,
  "culprit": "HTMLButtonElement.rebuildList (http://127.0.0.1:8772/:25:19)",
  "how": "assigned",
  "assignments": [{"from": 1820, "to": 0, "source": "HTMLButtonElement.rebuildList (…:25:19)"}],
  "user_actions": ["click Refresh list"]
}
```

The method is fixed rather than improvised: **measure** the value the complaint is about, **arm** the recorders before touching anything, **act** the way the user did, **collect**, then **report**. Arming late sees nothing, so the order is the point.

`arm` installs two recorders at once, because the two failure modes look identical from outside and only one of them leaves a trail:

- assignments, each with the call stack that made it — this is what names the culprit;
- a sampler for changes **nobody assigned** — a value also resets when a node is re-attached or re-laid-out, and no stack exists for that.

It drives the page like a person: real clicks, a real wheel, and text typed one character at a time (11 characters means 11 `keydown` events, not one `input`). Debounce, input handlers and autocomplete all behave differently for a value that appears all at once, so a bug that only shows up under real typing still shows up here. Since the accessibility tree carries roles and text but no selectors, every step is also handed the page's scrollable elements and what each is called.

**Findings come from the evidence, not from the model's closing statement.** A stack that was never captured cannot appear in `culprit`, and "cannot reproduce" is reported as itself rather than dressed up as an answer. Exit code is 0 when the bug was reproduced, 2 when it was not — nothing was learned in that case, which is the outcome worth failing on.

`examples/scroll_bug_fixture.html` is a target to try it against: three ways of losing a scroll position, two of which are indistinguishable from outside and are caught by different halves of `arm`.

## MCP Server

Zero-dependency stdio MCP server: `python3 -m vco.mcp_server` (plugin config in `plugins/vco/.mcp.json`). Exposes 8 tools:

- `web_snapshot` / `web_screenshot` / `web_click` / `web_run`: the headless web channel (aria-tree perception, screenshots, DOM clicks, agent loop) — no model required, or a text model for the loop;
- `screen_probe` / `screen_run`: the desktop channel (GLM/MiniMax vision models + grid zoom); requires `VCO_GLM_SETTINGS` / `VCO_MINIMAX_SETTINGS` env vars pointing at settings files;
- `cache_status` / `cache_clear`: cache management.

Any MCP-capable agent (Codex, Claude Code, Kimi, ...) can call these directly.

## Installation

Requires Python 3.9+.

### From PyPI (recommended)

```bash
pip install 'vco[control,ocr,browser]'
```

- `control` — real mouse control via `pyautogui`
- `ocr` — local OCR via `RapidOCR`
- `browser` — headless Chromium via `playwright` (also run `playwright install chromium`)

Minimal installs:

```bash
pip install vco              # core only: screenshot, grid, coordinate math
pip install 'vco[openai]'    # plus OpenAI-compatible provider
pip install 'vco[browser]'   # plus Playwright; still needs `playwright install chromium`
```

### From GitHub

```bash
git clone https://github.com/lo2589/vco.git
cd vco
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[control,ocr,browser]'
```

### First run on macOS

`vco` needs **Screen Recording** and **Accessibility** permissions. The first time a command touches the screen or mouse, macOS will prompt you. If it fails silently, go to **System Settings → Privacy & Security → Screen Recording / Accessibility** and add your terminal (or host app).

## Provider settings

Cloud providers (`minimax`, `glm`) read API keys from JSON files so keys never enter the repo or shell history. Copy the examples and fill in your keys:

```bash
cp examples/provider_settings.minimax.json.example provider_settings.minimax.json
cp examples/provider_settings.glm.json.example provider_settings.glm.json
```

`provider_settings.minimax.json`:

```json
{
  "api_key": "YOUR_MINIMAX_API_KEY_HERE"
}
```

`provider_settings.glm.json`:

```json
{
  "api_key": "YOUR_GLM_API_KEY_HERE",
  "base_url": "https://open.bigmodel.cn/api/paas/v4/"
}
```

The benchmark harness (`debug/run_web_ui_matrix.py`) also looks for `provider_settings.deepseek.json` in the settings directory; see `examples/provider_settings.deepseek.json.example` for that format. All `provider_settings.*.json` files are ignored by `.gitignore` so real keys cannot be committed by accident.

## How the desktop grid works

Grid strategy depends on model capability: small models use a two-level sparse grid; strong vision models read a single dense grid directly.

```text
screenshot of the target region
    │
    v
full-view 4×4 numbered grid ──> model picks a coarse cell
    │
    v
crop a 2×2-cell window around the coarse cell
    │
    v
re-slice the crop into 4×4 ──> model picks a fine cell
    │
    v
locally take the fine-cell center and map to real screen coordinates
    │
    v
click ──> wait for UI change ──> screenshot again
```

The model receives images and a task description, never numeric screen coordinates; it may only output strict JSON grid actions. Real screen coordinates are generated locally.

`MiniMax-M3` and `glm-4.6v` use adaptive zoom: the `model` strategy lets the model click early when the target is clear; the `fixed` strategy forces a cell pick and zoom at every non-final level and only accepts a click at the last level. On real desktops with tiny icons, model-chosen zoom cells can drift — prefer `fixed` for small or similar-looking controls.

Near screen edges the crop window shifts inward instead of shrinking. The second level defaults to the fine-cell center rather than the small model's unreliable `0–1` fractional offsets.

## Benchmarks

### Capability threshold

Success requires: a model that truly accepts image input; a coarse-to-fine grid so small models never read dense numbering; local code doing screenshots, coordinate math, and mouse actions; and bounded regions, steps, and action types.

| Model | Size | Image input | Result |
|---|---:|---:|---|
| `minicpm-v4.6:latest` | 752M LM + 548M vision projector, 1.6GB file | yes | 6/10 strict hits on 10 images, 2.67 s/image |
| `gemma4:latest` | 8B, 9.6GB file | yes (image & audio) | 6/10 strict hits, 16.27 s/image |
| `qwen3.6:latest` | 36B MoE, 23GB file | yes | not benchmarked yet |
| `RogerBen/HY-MT2-1.8B:latest` | 1.8B | no, completion-only | unusable for screen grounding |
| MiniMax `MiniMax-M2.5` API | paid cloud model | no; M2.x is text+tools only | failed the single-image capability gate |
| MiniMax `MiniMax-M3` API | paid cloud natively multimodal | yes | single-pass 16×16, 10/10 strict hits, 10.035 s/image |
| Zhipu `glm-4.6v` API | cloud vision model | yes | real-desktop "拼" icon hit `(1601,19)` with fixed 3-level zoom, 44.667 s |

The accurate conclusion is not "any 1.8B can operate a screen" but: **a capable small vision model — even below 1.8B — can perform limited screen clicks with grid zoom and a local controller.**

### 10-image benchmark

`debug/` contains 10 synthetic UI screenshots with ground-truth button bounds, covering different positions, colors, labels, and distractor buttons. All runs use `grid-only + two-level zoom`:

| Model | Strict hits | Mean latency | 20px expanded-box diagnostic |
|---|---:|---:|---:|
| `minicpm-v4.6:latest` | **6/10 (60%)** | **2.67 s** | 10/10 |
| `gemma4:latest` | **6/10 (60%)** | 16.27 s | 9/10 |
| MiniMax `MiniMax-M3` (single 16×16) | **10/10 (100%)** | 10.035 s | 10/10 |

Misses are all near-misses 8–28px from the button edge; the expanded-box score only indicates the model found the target area. Bigger models did not improve strict accuracy. MiniMax-M3's 10/10 comes from the synthetic set and does not guarantee the same rate on complex real desktops; cloud latency is still unsuitable for real-time control.

See [debug/README.md](debug/README.md) and the per-model `debug/results*/SUMMARY.md` for case overviews, trajectories, and machine-readable results. Full ablation notes: [docs/local-model-findings.md](docs/local-model-findings.md). `cache/web-ui-matrix/` has a separate 10-page 2560×1440 real web-UI comparison across models and strategies (including OCR-first).

### OCR-first is the best path for text targets

When the target has text, a unique OCR hit gives the center point locally: zero model calls, ~2–3 s, and no thumbnail misestimation. In the web-ui-matrix scoring, OCR-first groups reached 100% (9/9). The only scenario where pure-vision grid zoom is irreplaceable is a target with no text at all (bare icons, color blocks, custom-drawn controls).

## Full loop: run & probe

Static-image validation (no capture, no mouse):

```bash
python3 examples/generate_probe_fixture.py --output /tmp/vco-clean.png
vco probe \
  --task 'Click the blue Save button.' \
  --image /tmp/vco-clean.png \
  --provider ollama --model minicpm-v4.6:latest \
  --ollama-image-mode grid --zoom
```

Live loop (dry-run by default; add `--execute` for real clicks once coordinates look stable):

```bash
vco run \
  --task '点击保存按钮，看到成功状态后结束' \
  --region 100,100,1200,800 \
  --provider ollama --model minicpm-v4.6:latest \
  --ollama-image-mode grid --zoom \
  --max-steps 5            # add --execute once verified
```

MiniMax-M3 / GLM-4.6V usage (adaptive 16×16 zoom; do NOT pass `--zoom`):

```bash
vco run --task '...' --region 100,100,1000,600 \
  --provider minimax --minimax-settings /path/to/provider_settings.minimax.json \
  --max-steps 1
# verify cache/runs/<timestamp>/step-001-action.json, then:
#   --max-steps 20 --settle 1.0 --execute
```

Recommended GLM flags for tiny icons: `--model glm-4.6v --glm-thinking disabled --glm-image-mode both --adaptive-zoom-strategy fixed --adaptive-zoom-levels 3`.

Common zoom/model flags: `--adaptive-zoom-levels 3`, `--adaptive-zoom-span 4`, `--adaptive-zoom-strategy model|fixed`, `--no-adaptive-zoom`, `--minimax-service-tier standard|priority`, `--minimax-image-detail low|default|high`, `--zoom-use-model-offset`, `--zoom-center-delta`.

API keys are read only from settings JSON files and are never copied into the repo or run artifacts.

## Action protocol

```json
{"type": "click", "target": {"cell": 11, "offset_x": 0.5, "offset_y": 0.5}}
{"type": "drag", "start": {"cell": 6, "offset_x": 0.2, "offset_y": 0.5},
 "end": {"cell": 10, "offset_x": 0.8, "offset_y": 0.5}}
{"type": "done", "reason": "已看到保存成功状态"}
```

Cells are one-based, row-major. `offset_x`/`offset_y` must be in `[0,1]`. Extra fields and raw screen coordinates are rejected by the schema.

## OCR interface

OCR is decoupled from vision models and the default backend is cross-platform. `rapidocr` (cross-platform ONNX, offline) is preferred; `apple-vision` is a macOS-only optional backend; `http` talks to any OCR service implementing the VCO JSON contract; `auto` tries RapidOCR first, then platform backends.

```bash
vco ocr screen.png --backend rapidocr --mode accurate \
  --language zh-Hans --language en-US --output result.json
vco ocr-serve --backend rapidocr --host 127.0.0.1 --port 8765   # built-in /v1/ocr service
```

Python interface:

```python
from PIL import Image
from vco.ocr import OCRRequest, create_ocr_backend

backend = create_ocr_backend("auto")
result = backend.recognize(
    Image.open("screen.png"),
    OCRRequest(languages=("zh-Hans", "en-US"), mode="accurate"),
)
for box in result.boxes:
    print(box.id, box.text, box.confidence, box.bbox, box.center)
# result.find_text("拼") matches text locally and returns the click center
```

Third-party backends can register via `register_ocr_backend("my-ocr", MyOCRBackend)` without touching the core. An OCR service bound to a non-loopback address must set a Bearer token via `--api-key-env`.

OCR + vision-model composition (shared by `probe`/`run`/`find`/`click`): a unique match locates locally (`model_calls=0`); zero or multiple matches pass OCR text and grid positions to the model as hints; OCR failure falls back to the plain vision model. `--ocr-match contains` relaxes matching; `--no-ocr-direct-click` forbids OCR-decided clicks.

## Other commands

```bash
vco overlay screenshot.png --output grid.png --rows 10 --cols 10   # draw a grid
vco overlay screenshot.png --output grid.png --mapping mapping.json \
  --region 100,200,1200,800                                        # coordinate mapping table
vco convert --region 100,200,1200,800 --rows 10 --cols 10 \
  --action '{"type":"click","target":{"cell":45,"offset_x":0.5,"offset_y":0.5}}'
                                                                   # offline action resolution
```

Also supports manual input (`--provider manual`), JSONL replay, and the OpenAI provider — see `vco --help`.

## Capability boundaries

Good at: clicking clear buttons, cards, and icons; coarse selection inside bounded regions; simple straight-line drags; low-cost local Computer-Use proofs of concept; acting as a "visual probe" for agents (screenshot, locate, verify).

Not yet reliable or unsupported: text-dense UIs, very small or visually similar controls; fast-disappearing menus, heavy animation, low-latency reactions; scrolling, multi-point curves, complex drawing; precise dragging; autonomously understanding long tasks (small models may misfire `done` — always set max steps); safely executing payments, deletions, message sending, and other high-risk operations.

## Safety design

- `run` defaults to dry-run; real mouse input requires explicit `--execute`. `find` never moves the mouse; only `click` does.
- Ambiguous OCR targets refuse to click and fall back to model judgment.
- All actions are confined to the user-specified region.
- Model JSON is validated against a strict schema; illegal offsets are never silently corrected.
- `--max-steps` prevents infinite loops.
- The action set contains no shell, file operations, or arbitrary tool calls; `type`/`fill` use the keyboard only to enter user-supplied text into located input fields.
- Every step saves the clean image, grid image, zoom images, model action, and local resolution.
- Ollama defaults to `127.0.0.1:11434`; images never leave the machine with that provider.
- Real execution uses pyautogui with the top-left-corner fail-safe enabled: slam the mouse into the main screen's top-left corner to emergency-stop.

## Artifacts

- `shot`/`find`/`click`/`ask` artifacts go to `.screenshot/` under the current directory (already in `.gitignore`).
- `run` writes full trajectories to `cache/runs/<timestamp>/`: task, mapping table, per-step screenshots, zoom images, actions, and resolved coordinates.

These files may contain private screen content; both directories can be deleted wholesale:

```bash
rm -rf ./cache/ ./.screenshot/
```

## Tests

```bash
python3 -m unittest discover -v
```

Covers grid boundaries, forward/reverse coordinate mapping, strict JSON schema, Ollama/OpenAI/MiniMax transports, edge cropping, two-level zoom, and loop artifacts; plus the web agent's decision parsing (refusing precisely matters as much as accepting — a rejected decision is handed back for the model to correct) and the report builder behind `webdebug`, including one test pinning down the rule that a cause the model merely asserted, with nothing recorded behind it, never reaches the findings.

## Next steps

1. Image diffing before/after actions for success detection, reducing "clicked but nothing happened" misjudgments (today's stopgap is a manual/model `shot` comparison after `click`).
2. Forbidden-region policies and pre-execution confirmation for high-risk areas.
3. Separate local zoom for drag start and end points.
4. An automated grounding evaluation set with varied positions, sizes, and themes.
5. A window picker for live runs to avoid hand-writing `--region`.
