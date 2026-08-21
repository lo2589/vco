---
name: operate-screen
description: 用 vco CLI 截图、定位屏幕上的控件（OCR 优先，视觉模型兜底）、画红圈确认并真实点击鼠标。当用户要求查看屏幕现状、找到某个按钮/文字/控件、点击界面元素、验证点击结果时使用。
whenToUse: 用户要求观察屏幕、定位界面元素、点击按钮或验证 UI 操作时
---

# Operate Screen —— 给 LLM 用的简易点击器

`vco` 是一个命令行点击器：截图、定位、确认、点击，全部通过 shell 命令完成，stdout 恒为 JSON。不绑定任何特定 agent——任何会跑 shell 命令、会看图片的 LLM 都能用。

## 安装

```bash
pip install -e '.[control,ocr]'           # control=真实鼠标(pyautogui)，ocr=RapidOCR
pip install playwright && playwright install chromium   # 仅 webshot 需要
```

## 命令

所有产物写到**当前工作目录**的 `.screenshot/`，从哪个项目运行就存到哪个项目。

| 命令 | 作用 | 副作用 |
|---|---|---|
| `vco shot [--region x,y,w,h] [--display N]` | 截图，返回 JSON 路径；`--display 2` 选副屏（默认主屏），输出坐标为全局逻辑坐标 | 无 |
| `vco webshot <url> [--full-page]` | 无头 Chromium 渲染网页并截图（需 playwright）；输出含 `console_errors`/`page_errors`/`failed_requests` | 无 |
| `vco webtext <url>` | 导出页面无障碍树（角色+文字 YAML），纯文本模型用它"看"页面；同样带三个错误字段 | 无 |
| `vco webrun <url> --task "任务" --provider ollama --model qwen3:8b` | agent 闭环：文本模型读 aria 树自主决策多步操作（click/fill/done），直到任务完成或 `--max-steps`；全程零视觉模型；留档 JSON + 最终截图 | 网页内点击/填表 |
| `vco webclick <url> --target "文字" [--fill 占位符=内容] [--profile 目录] [--headed --hold N]` | 无头页面内 DOM 操作：先填表再点击；唯一命中才点，多候选拒绝并列出；前后截图留档；不碰真实鼠标；`--profile` 持久化登录态；`--headed --hold N` 弹窗演示并停留 N 秒（点击前橙色光圈标记目标）；`--selector` 用 CSS/Playwright 选择器点任意元素 | 网页内点击 |
| `vco ask [图片] --provider ... --model ...` | 问视觉模型"图里有什么"；不传图则先截屏；`--question` 自定义问题 | 无 |
| `vco find --target "文字" [--display N]` | OCR 定位，画半透明红圈，返回 x/y | 无（dry-run） |
| `vco find --task "描述" --provider ...` | 无文字目标走视觉模型网格定位 | 无（dry-run） |
| `vco click --target "文字"` | 定位并真实点击 | **移动鼠标** |
| `vco click --at x,y` | 直接点击坐标 | **移动鼠标** |

`find`/`click` 输出 JSON 关键字段：

- `found`：是否定位成功（false 时退出码为 2，是正常结果不是故障）
- `x` / `y`：逻辑屏幕坐标
- `method`：`ocr`（零模型调用）/ `model` / `model+ocr-hints`
- `marked_image`：画了红圈的确认图
- `metadata.elapsed_seconds`：耗时

## 工作流（务必按序）

1. **看现状**：`vco shot`，用你的图片读取工具查看截图。
2. **定位**：`vco find --target "按钮文字"`。目标无文字时用 `--task "描述" --provider ollama --model minicpm-v4.6:latest --zoom --ollama-image-mode grid`（本地小模型），或 `--provider minimax/glm`（云端强模型，需 settings 文件）。
3. **确认红圈**：打开输出 JSON 里的 `marked_image`，确认半透明红圈正中目标。偏了就换策略重来，不要硬点。
4. **点击**：红圈正确后 `vco click --at <x>,<y>`。
5. **验证**：再 `vco shot`（网页则 `vco webshot` 同一 URL）确认界面发生了预期变化。没生效就如实报告，不要假装成功。

## 网页走无头通道（优先于屏幕操作）

目标是网页时，不要用屏幕截图那套，用 DOM 通道——确定性、不占前台、不碰真实鼠标：

- **看**：`vco webshot <url>` 渲染截图；没有看图能力的文本模型用 `vco webtext <url>`，输出无障碍树（`button "发送"`、`textbox "输入消息"` 这种角色+文字结构），比截图省 token 也更准。
- **点/填**：`vco webclick <url> --target "文字" [--fill 占位符=内容]`，先填表再点击；唯一命中才点，多候选会拒绝并列出候选。
- **排障**：`webshot`/`webclick` 的输出自带 `console_errors`、`page_errors`、`failed_requests` 三个字段——页面异常先看它们，不要盯着截图猜。
- **登录态**：加 `--profile <目录>` 持久化 Cookie/localStorage。
- **给人演示**：加 `--headed --hold 3` 弹出真实浏览器窗口，用户看着动作执行，结束后停留 N 秒。`--hold` 期间命令会阻塞，纯自动化场景不要加。

## 演示模式工作流（给人看的标准流程）

1. **先感知**：`vco webtext <url>` 读无障碍树，找到输入框 placeholder 和按钮文字。不看截图。
2. **再演示**：`vco webclick <url> --fill "占位符=内容" --target "按钮" --headed --hold 6 --expect "发出去的内容"`。headed 模式自动带演示节奏：输入框**逐字打字**（90ms/字，接近真人打字速度，观众能看到字出现）→ 停 400ms → 点击前**橙色光圈**标记目标停 0.9 秒 → 点击 → `--expect` 等内容真正出现在页面上（`verified: true/false` 进 JSON）→ 窗口停留 `--hold` 秒再退场。**演示时 `--hold` 至少 5 秒**，给观众看清结果的时间。
3. **后验证**：再 `vco webtext <url>`，确认内容进了页面状态。
4. **要录像**：以上任何网页命令加 `--record`，整段操作存成 `.webm` 到 `.screenshot/`——无头模式同样会录进逐字打字和橙色光圈，直接就是可发布的 demo 视频。

## 歧义与失败处理

- OCR 多个命中时工具**拒绝点击**（`found:false`），避免随机点错。换更精确的 `--target`（带上下文文字，如用 `"grid16.png"` 而不是 `"grid16"`），或加 `--provider` 让模型在候选中判断。
- OCR 零命中且未配置 provider：目标可能无文字，改用 `--task` + 视觉模型。
- 截图会过期：`find` 与 `click` 之间界面可能已变化，间隔久或界面动态时重新走完整流程。
- 精确匹配是默认行为；目标文字可能被 UI 截断或带空格，可加 `--ocr-match contains`。

## 安全

- 永远先 `find` 看红圈，再 `click`；不要跳过确认直接点。
- 付款、删除、发消息、账号变更等高风险操作，必须用户明确授权该具体动作后才能 `click`。
- 用 `--region` 限定观察和点击范围；不要无授权操作全屏。
- 鼠标失控时把鼠标快速移到屏幕左上角触发 pyautogui fail-safe 紧急停止。
