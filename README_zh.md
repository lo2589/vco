# Visual Computer Operate (vco)

[English README](README.md)

给 LLM 用的点击器：让任何会跑 shell 命令、会读 JSON 的 agent 操作网页和桌面。

两条通道，确定性优先，视觉模型只兜底：

- **网页**：无头 Chromium 走 DOM——aria 树感知（文本模型可读）、按文字/选择器点击、填表、报错采集、录像，全程零视觉模型；
- **桌面**：截图走 OCR 定位（唯一命中零模型调用），无文字目标才让视觉模型在编号网格上选格，本地程序负责坐标换算和真实鼠标。

## 命令一览

stdout 始终是 JSON，产物默认写到**当前工作目录**的 `.screenshot/`（从哪个项目运行就存到哪个项目）。

网页（无头，不碰真实鼠标）：

```bash
vco webshot https://example.com   # 渲染网页并截图；--full-page 整页
                                  # 输出含 console_errors/page_errors/failed_requests，调前端直接看报错
vco webtext https://example.com   # 导出页面无障碍树（角色+文字的 YAML），纯文本模型用它"看"页面
vco webclick http://127.0.0.1:9005 --fill 输入消息=你好 --target 发送
                                  # 填表（可多个 --fill）再点击；多候选拒绝并列出；前后截图留档
                                  # --expect "文字" 点击后等内容出现（verified 字段）
                                  # --profile <目录> 持久化登录态；--selector 用 CSS 选择器点任意元素
                                  # --headed --hold 3 弹窗演示（逐字打字、点击前橙色光圈标记目标）
                                  # --record 录制整段操作视频（.webm），无头也能录
vco webrun http://127.0.0.1:9005 --task '输入xxx并点击发送' --provider ollama --model qwen3:8b
                                  # 文本模型自主多步操作：快照→模型决策→DOM 执行→循环直到 done
```

桌面（OCR 优先，真实鼠标只在 click 时动）：

```bash
vco shot                          # 截图；--region x,y,w,h 截局部；--display 2 选副屏（默认主屏）
                                  # 输出坐标始终是全局逻辑坐标，click 直接可用
vco find --target "CODEX"         # 定位（dry-run）：OCR 优先，命中点画半透明红圈
vco click --at 1164,92            # 真实点击坐标；click --target "..." 则先定位再点
vco ask [图片] --provider ollama --model minicpm-v4.6:latest
                                  # 问视觉模型"图里有什么"；不传图片则先截屏
```

桌面推荐工作流（每步都可验证）：

```text
shot            看现状
find --target   定位 + 红圈确认图（marked_image），不动鼠标
  │             ├─ OCR 唯一命中 → 本地直接出坐标，零模型调用
  │             ├─ OCR 多个命中 → 拒绝点击，换更精确的 target 或加 --provider 让模型判断
  │             └─ OCR 零命中   → 加 --provider 走视觉模型网格/变焦多步判断
click --at      确认红圈位置正确后执行真实点击
shot            再截图验证界面真的变了
```

网页更简单：`webtext` 感知 → `webclick` 操作（`--expect` 自带验证）；或交给 `webrun` 让文本模型自己跑完整个任务。

`find`/`click` 输出 JSON 关键字段：`found`（false 时退出码 2，是正常结果不是故障）、`x`/`y`、`method`（`ocr` / `model` / `model+ocr-hints`）、`marked_image`、`metadata.elapsed_seconds`。

`ask` 支持四个 provider：`ollama`（本地，默认 `127.0.0.1:11434`）、`minimax` 和 `glm`（需 `--minimax-settings` / `--glm-settings` 指向含 api_key 的 JSON）、`openai`。可用 `--question` 自定义问题。

完整的使用说明（含安全规则和失败处理）写成了工具无关的 agent skill：[`skills/operate-screen/SKILL.md`](skills/operate-screen/SKILL.md)，可直接接入各 agent 的 skills 目录；本仓库的 `.kimi-code/skills/operate-screen` 是指向它的软链，`plugins/visual-computer-operate/` 是给 Codex 的 MCP 版本。

## MCP Server

零依赖 stdio MCP server：`python3 -m vco.mcp_server`（插件配置见 `plugins/visual-computer-operate/.mcp.json`）。暴露 8 个工具：

- `web_snapshot` / `web_screenshot` / `web_click` / `web_run`：网页无头通道（aria 树感知、截图、DOM 点击、agent 闭环），无需模型或配文本模型；
- `screen_probe` / `screen_run`：桌面屏幕通道（GLM/MiniMax 视觉模型 + 网格变焦），需 `VCO_GLM_SETTINGS` / `VCO_MINIMAX_SETTINGS` 环境变量指向 settings 文件；
- `cache_status` / `cache_clear`：缓存管理。

任何支持 MCP 的 agent（Codex、Claude Code、Kimi 等）接入后都能直接调用。

## 安装

要求 Python 3.9+。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[control,ocr,browser]'   # control=真实鼠标(pyautogui)，ocr=RapidOCR，browser=Playwright
```

按需裁剪：`pip install -e .` 只有核心；`.[openai]` 是 OpenAI provider；`.[browser]` 装完还需 `playwright install chromium`（本机已有 Chromium 缓存则跳过）。macOS 首次运行需要在"系统设置 → 隐私与安全性"中为终端开启**屏幕录制**和**辅助功能**。

## 工作方式

网格策略按模型能力分流：小模型使用两级稀疏网格，强视觉模型直接使用单轮密集网格。

```text
目标区域截图
    │
    v
全图 4×4 编号网格 ──> 模型选择粗格
    │
    v
截取粗格附近 2×2 大小的区域
    │
    v
局部重新切成 4×4 ──> 模型选择细格
    │
    v
本地取细格中心并换算为真实屏幕坐标
    │
    v
点击 ──> 等待界面变化 ──> 再次截图
```

模型收到图片和任务描述，但不会收到数值形式的屏幕坐标；模型输出也只能是严格 JSON 网格动作。真实屏幕坐标只在本地生成。

对 `MiniMax-M3` 和 `glm-4.6v` 使用自适应变焦：`model` 策略允许模型在目标足够清晰时提前 `click`；`fixed` 策略每个非末级都要求给出目标格并固定放大，末级才接受点击。真实桌面小图标测试中模型自选的变焦格可能偏离目标，小型或相似控件优先 `fixed`。

靠近屏幕边缘时裁剪窗口整体向内移动，不会缩小。第二级默认使用细格中心，不依赖小模型不稳定的 `0–1` 小数偏移。

## 实测结果

### 模型能力门槛

成功需要同时满足：模型真正支持图片输入；使用粗到细的网格避免小模型读密集编号；本地程序执行截图、坐标换算和鼠标动作；操作区域、轮数和动作种类受限。

| 模型 | 参数/文件规模 | 图片能力 | 结果 |
|---|---:|---:|---|
| `minicpm-v4.6:latest` | 752M 语言模型 + 548M 视觉投影，1.6GB 文件 | 有 | 10 图严格命中 6/10，2.67 秒/图 |
| `gemma4:latest` | 8B 参数、9.6GB 文件 | 有（图像与音频） | 10 图严格命中 6/10，16.27 秒/图 |
| `qwen3.6:latest` | 36B MoE、23GB 文件 | 有 | 尚未跑基准 |
| `RogerBen/HY-MT2-1.8B:latest` | 1.8B 参数 | 无，仅 completion | 不能用于屏幕视觉定位 |
| MiniMax `MiniMax-M2.5` API | 云端付费模型 | 无；M2.x 仅支持文本与工具 | 单图能力门槛失败 |
| MiniMax `MiniMax-M3` API | 云端付费原生多模态模型 | 有 | 单轮 16×16 严格命中 10/10，10.035 秒/图 |
| 智谱 `glm-4.6v` API | 云端视觉模型 | 有 | 真实桌面"拼"图标固定三级命中 `(1601,19)`，44.667 秒 |

准确结论不是"任意 1.8B 都能操作屏幕"，而是：**一个能力合适的小视觉模型，即使不足 1.8B，也能在网格变焦和本地控制器辅助下完成有限的屏幕点击。**

### 10 张图基准

`debug/` 中有 10 张带真实按钮边界的合成 UI 截图，覆盖不同目标位置、颜色、文字和干扰按钮。统一使用 `grid-only + 两级变焦`：

| 模型 | 严格命中 | 平均耗时 | 20px 扩框诊断 |
|---|---:|---:|---:|
| `minicpm-v4.6:latest` | **6/10（60%）** | **2.67 秒/张** | 10/10 |
| `gemma4:latest` | **6/10（60%）** | 16.27 秒/张 | 9/10 |
| MiniMax `MiniMax-M3`（单轮 16×16） | **10/10（100%）** | 10.035 秒/张 | 10/10 |

失败均为距离按钮边界 8–28px 的近失误；扩框分数仅用于判断模型是否找到了目标附近。当前结果没有显示增大模型能提高严格定位准确率。MiniMax-M3 的 10/10 是合成测试集结果，不代表复杂真实桌面也有相同成功率；云端调用延迟仍不适合实时控制。

测试图总览、模型轨迹与机器可读结果见 [debug/README.md](debug/README.md) 和各 `debug/results*/SUMMARY.md`。完整消融记录见 [docs/local-model-findings.md](docs/local-model-findings.md)。`cache/web-ui-matrix/` 另有 10 张 2560×1440 真实网页界面的多模型、多策略对比（含 OCR-first）。

### OCR-first 是文字目标的最优路径

目标带文字时，OCR 唯一命中直接本地算中心点：零模型调用、约 2–3 秒、精度不受缩略图估位误差影响。web-ui-matrix 评分中 OCR-first 组达到 100%（9/9）。纯视觉网格变焦真正不可替代的场景只剩一种：目标没有任何文字（纯图标、色块、自绘控件）。

## 完整闭环：run 与 probe

静态图片验证（不截图、不动鼠标）：

```bash
python3 examples/generate_probe_fixture.py --output /tmp/vco-clean.png
vco probe \
  --task 'Click the blue Save button.' \
  --image /tmp/vco-clean.png \
  --provider ollama --model minicpm-v4.6:latest \
  --ollama-image-mode grid --zoom
```

实时闭环（默认 dry-run，确认坐标稳定后加 `--execute` 真实点击）：

```bash
vco run \
  --task '点击保存按钮，看到成功状态后结束' \
  --region 100,100,1200,800 \
  --provider ollama --model minicpm-v4.6:latest \
  --ollama-image-mode grid --zoom \
  --max-steps 5            # 确认后加 --execute
```

MiniMax-M3 / GLM-4.6V 用法（自适应 16×16 变焦，不要加 `--zoom`）：

```bash
vco run --task '...' --region 100,100,1000,600 \
  --provider minimax --minimax-settings /path/to/provider_settings.minimax.json \
  --max-steps 1
# 确认 cache/runs/<时间戳>/step-001-action.json 坐标正确后：
#   --max-steps 20 --settle 1.0 --execute
```

GLM 小图标推荐参数：`--model glm-4.6v --glm-thinking disabled --glm-image-mode both --adaptive-zoom-strategy fixed --adaptive-zoom-levels 3`。

常用变焦/模型参数：`--adaptive-zoom-levels 3`、`--adaptive-zoom-span 4`、`--adaptive-zoom-strategy model|fixed`、`--no-adaptive-zoom`、`--minimax-service-tier standard|priority`、`--minimax-image-detail low|default|high`、`--zoom-use-model-offset`、`--zoom-center-delta`。

API key 只从 settings JSON 读取，不会复制进仓库或运行结果。

## 动作协议

```json
{"type": "click", "target": {"cell": 11, "offset_x": 0.5, "offset_y": 0.5}}
{"type": "drag", "start": {"cell": 6, "offset_x": 0.2, "offset_y": 0.5},
 "end": {"cell": 10, "offset_x": 0.8, "offset_y": 0.5}}
{"type": "done", "reason": "已看到保存成功状态"}
```

编号从 1 开始，按行优先。`offset_x`/`offset_y` 合法范围 `[0,1]`。协议禁止额外字段和原始屏幕坐标。

## OCR 接口

OCR 与视觉模型解耦，默认实现不依赖 macOS。`rapidocr`（跨平台 ONNX，本地离线）是首选；`apple-vision` 仅为 macOS 可选后端；`http` 可接任意实现 VCO JSON 合同的 OCR 服务；`auto` 优先 RapidOCR 再尝试平台后端。

```bash
vco ocr screen.png --backend rapidocr --mode accurate \
  --language zh-Hans --language en-US --output result.json
vco ocr-serve --backend rapidocr --host 127.0.0.1 --port 8765   # 自带 /v1/ocr 服务
```

Python 接口：

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
# result.find_text("拼") 本地精确匹配文字并取得点击中心
```

第三方后端可通过 `register_ocr_backend("my-ocr", MyOCRBackend)` 接入，不需要修改核心。绑定非本机地址的 OCR 服务必须 `--api-key-env` 设置 Bearer Token。

OCR 与视觉模型的组合策略（`probe`/`run`/`find`/`click` 通用）：唯一匹配直接本地定位（`model_calls=0`）；零个或多个匹配时把 OCR 文字及其网格位置作为提示交给模型判断；OCR 失败自动退回纯视觉模型。可用 `--ocr-match contains` 放宽匹配，`--no-ocr-direct-click` 禁止 OCR 直接决定点击。

## 其他命令

```bash
vco overlay screenshot.png --output grid.png --rows 10 --cols 10   # 画网格
vco overlay screenshot.png --output grid.png --mapping mapping.json \
  --region 100,200,1200,800                                        # 坐标映射表
vco convert --region 100,200,1200,800 --rows 10 --cols 10 \
  --action '{"type":"click","target":{"cell":45,"offset_x":0.5,"offset_y":0.5}}'
                                                                   # 离线换算动作
```

也支持人工输入（`--provider manual`）、JSONL replay 和 OpenAI provider，详见 `vco --help`。

## 当前能力边界

适合：较明显的按钮、卡片、图标点击；受限区域内的粗粒度选择；简单两点直线拖拽；本地低成本 Computer-Use 概念验证；给 agent 当"视觉探针"（截图、定位、验证）。

暂不可靠或尚未支持：文字密集、控件非常小或视觉相似度很高的界面；菜单快速消失、动画频繁、需要低延迟反应的操作；键盘输入、滚动、多点曲线和复杂绘画；精确拖拽；自动理解任意长任务（小模型可能误判 `done`，必须设最大轮数）；安全执行付款、删除数据、发送消息等高风险操作。

## 安全设计

- `run` 默认 dry-run；真实鼠标输入必须显式 `--execute`。`find` 永不动鼠标，`click` 才会点击。
- OCR 多目标歧义时拒绝直接点击，退回模型判断。
- 所有动作限制在用户指定的目标区域内。
- 模型 JSON 使用严格 schema 校验，非法偏移不会被静默修正。
- `--max-steps` 防止闭环无限运行。
- 动作集合不包含 shell、文件操作、键盘输入或任意工具调用。
- 每轮保存干净图、网格图、变焦图、模型动作和本地解析结果。
- Ollama 默认连接 `127.0.0.1:11434`，图片不发送给云端。
- 真实执行使用 pyautogui，左上角 fail-safe 保持开启：紧急停止时把鼠标快速移到主屏幕左上角。

## 运行留档

- `shot`/`find`/`click`/`ask` 的产物写入**当前目录**的 `.screenshot/`（已加入 `.gitignore`）。
- `run` 的完整轨迹写入 `cache/runs/<timestamp>/`：原始任务、映射表、每轮截图、变焦图、动作和解析坐标。

这些文件可能包含屏幕隐私信息，`cache/` 与 `.screenshot/` 都可整体删除：

```bash
rm -rf ./cache/ ./.screenshot/
```

## 测试

```bash
python3 -m unittest discover -v
```

覆盖网格边界、正反向坐标映射、严格 JSON schema、Ollama/OpenAI/MiniMax 传输、边缘裁剪、两级变焦和闭环留档。

## 下一步

1. 动作前后图像差分和成功判定，减少"点了但没生效"的误判（`click` 后 `shot` 人工/模型比对是当前的临时方案）。
2. 对高风险区域增加禁止点击策略和执行前确认。
3. 对拖拽的起点、终点分别执行局部变焦。
4. 建立多位置、多尺寸、多主题控件的自动化定位评测集。
5. 为实时运行增加窗口选择器，减少手工填写 `--region`。
