# 交接：把 vco debug 接进 dsh

写给下一个接手的人。**这个文档只写我实测过的事实**，推测会明确标注。

---

## 1. 目标（用户原话）

> 1. 参考 dsh 的轨迹开个页面将 vco debug 集成进去
> 2. 精简页面，主要是 vco debug 的侧边栏精简一点，逻辑和之前一样但是废话少点，
>    ctrl+dom 点击之后开个小块，然后自动浮到最顶上，加入一个 insert 操作，
>    把信息直接插入到对话框
> 3. 插件和 chat 同等级
> 4. 和 cdd lens 一样放后面，不要放第一个
> 5. 不要浮层
> 6. 测试要带截图

---

## 2. 当前状态：一半成了，一半卡死

### 已经做完并且验证过的（可以信赖）

**A. vco 面板本身（`vco/debug_assets/`）—— 完成**

| 要求 | 实现 | 验证方式 |
|---|---|---|
| 侧边栏精简 | 400px → 360px，说明文字全删；URL / 错误 / 运行记录 / 导出 / 诊断收进 `<details>` | `tests/verify_panel_dom.py` |
| ctrl+点击 → 小块浮到最顶上 | 详情块是侧边栏第一块且 `position: sticky; top: -10px`；新 pick 自动回到顶部，滚动时不动 | 同上，含 `before → after` 偏移断言 |
| insert 操作 | 块内四按钮：选择器 / HTML / 文字 / 全部信息 → `window.parent.postMessage({source:'vco-debug-panel', ...})`；单独开标签页时退化为剪贴板 | 同上，含真实 payload 断言 |

`tests/verify_panel_dom.py` 在真实 Chromium 里注入合成 pick 消息，**16/16 通过**，实测 payload：

```json
{"source":"vco-debug-panel","kind":"insert-text","text":"button#submit.primary","selector":"button#submit.primary","insert":false}
```

跑法：`/tmp/vco-install-demo/venv/bin/python tests/verify_panel_dom.py`（需要面板服务在 8919）。

**B. 合规的 dsh bundle（`dsh-plugin-vco-debug/`）—— 写完，语法通过，但没装**

```
dsh-plugin-vco-debug/
├── package.json          main: src/host.js, exports ./client, dsh.bundle.patch, dsh.client.platform
├── cordis.patch.yml      insert: { id: vco-debug, name: dsh-plugin-vco-debug, config{...} }
└── src/
    ├── host.js           module.exports = { name:'vco-debug', inject:['timer','webServer'], apply }
    └── client.js         window.__ModuleLoader__.load({ id:'vco-debug', factory })
```

已验证：`node --check src/host.js` 通过；client.js 用假 loader 求值通过（id / factory 正确）；patch YAML 可解析。

**C. 面板服务 —— 修好了，在跑**

```
http://127.0.0.1:8919/  → 200，响应 2ms，socket 稳定，帧 115KB
```

修的是**启动时没给目标 URL** 这个坑（见第 4 节）。

### 没做完的（卡在这）

**动态 Cordis 插件那条路（`vcovw-1`）已证明走不通，不要再试。**

现在 `cordis_inspect_query` 查 `conversation.view`，注册表里**有** `dyn/vcovw-1 · vco-debug · order 30 · priority -1 · active: true`，但真实页面 DOM 里 `tabs: ['Chat','Trajectory','CDD Lens']`——**三个障碍，每个都足以致命**：

| # | 障碍 | 实测证据 |
|---|---|---|
| 1 | 守卫强制**负 priority**，永远排第一 | 我提交 `priority: 1`，落库是 `-1`。`ui-slots` 排序是 `(priority ?? 0) - (b.priority ?? 0) \|\| (order - b.order)`，priority 是第一键 → `order` 完全失效 |
| 2 | 标签列表**启动时算一次**，后注册进不去 | 我自己在 `apply` 里普查读到 `count: 4`，DOM 仍是 3。换视图、刷新页面、二次注册（还产生了重复条目）全试过 |
| 3 | 刷新即蒸发，**不会自动恢复** | host 侧 `running`，client 注册表空；必须再调一次 `cordis_run` 才推回来 |

**唯一出路是走 bundle（B）**：启动时注册 → priority 默认 0 → `order: 30` 生效 → 排在 cdd-lens(20) 后面。

---

## 3. 下一步：装 bundle（需要 `~/.dsh` 写权限）

沙箱里我实测写不了：

```
$ ln -sfn .../dsh-plugin-vco-debug ~/.dsh/profiles/web/node_modules/
ln: Operation not permitted
```

**三条命令（用户执行）：**

```bash
# 1. 链接进 profile
ln -sfn /Users/a1/Workspace/WORKSPACE/visual_computer_operate/dsh-plugin-vco-debug \
        ~/.dsh/profiles/web/node_modules/dsh-plugin-vco-debug

# 2. 编辑 ~/.dsh/profiles/web/package.json：
#    dependencies 加：
#      "dsh-plugin-vco-debug": "link:/Users/a1/Workspace/WORKSPACE/visual_computer_operate/dsh-plugin-vco-debug"
#    dsh.profile.bundles 数组加：
#      "dsh-plugin-vco-debug"

# 3. 重启 dsh web
```

**装完的验收标准：**

```
conversation.view occupants 应该是 4 个，vco-debug 在最后：
  chat(0) · trajectory(10) · cdd-lens(20) · vco-debug(30)
DOM: tabs = ['Chat','Trajectory','CDD Lens','VCO Debug']
```

bundle 加载失败会**明确报错**（不像动态包那样静默），把错误贴出来即可。

---

## 4. 踩过的坑（按代价排序，别重复踩）

### 4.1 `styles.insert(undefined)` 会静默中断整个 client apply

在动态包里，**在 `apply` 外部声明的变量不会跨边界传进 `apply`**。`var CSS = [...].join('')` 放在 `return {apply}` 之后，沙箱求值时没绑定，传进 `styles.insert` 的是 `undefined` → 抛错 → **后面所有 slot 注册一行都没执行**，而 runner 仍把 host 的 `apply` 完成当作成功。

**规则：`apply` 需要的一切都在它内部构造。**

### 4.2 `ctx.timeout` / `ctx.interval` 必须声明 `inject: ['timer']`

否则运行时被 guard 拒绝：

```
service "timer" is not declared by your plugin.
```

### 4.3 `harness.defineTool` 必须带 `output: { schema, render }`

```js
harness.defineTool({
  name, description,
  parameters: { type:'object', properties:{...}, required:[...] },
  output: { schema: { type:'json' }, render: (args, v) => [{ type:'text', text: JSON.stringify(v, null, 2) }] },
  execute: async (args) => ({...}),
})
```

`render` 必须返回 content block **数组**。缺 `output` 直接：

```
harness.defineTool output must declare { schema, render, presentationMeta? }
```

### 4.4 动态 Host 沙箱**没有 `process`**

实测 `typeof process === 'undefined'`，`child_process` / `http` / `fs` 全不可用。所以动态包**起不了 vco 服务**，必须外部起。真实 bundle（`src/host.js`）里这些都有，所以 bundle 能自己 `spawn` + 15 秒自愈。

### 4.5 面板服务不给目标 URL → 表现为「总在连接中」

这个是用户报的 bug，真相不是连接问题：

```
启动时没给 target:   targetUrl: about:blank    frameBytes: 6,520   ← 空白页
带 target 重启后:     targetUrl: http://127.0.0.1:3080/  frameBytes: 114,082
```

WebSocket 全程 `socket: connected`，没有断开横幅。**白画面被误读成连接不稳。** 所以 bundle 的 patch config 里带了 `target: http://127.0.0.1:3080/`。

### 4.6 孤儿注册：动态包死了，slot 注册还挂在页面上

DSH 重启后 `vcodsh-1`（更早的插件 id）从插件注册表消失，但它的 `shell.overlay` 注册**留在页面里**——一个浮窗一直粘在右下角，后端 `cordis_stop` 返回 `no dynamic plugin`，**清不掉**。**只有刷新页面能清。**

### 4.7 踩过就忘的规范（从真实 bundle 里读出来的）

- **entry id 三处必须一致**：patch 的 `id` = host 的 `name` = client `__ModuleLoader__.load` 的 `id`。cdd-lens 的注释原话：不一致就静默加载、什么都不做、**没有任何报错**。
- **`webServer` 必须写成硬依赖**：provider-quick-config 的注释——"bundle 加载早于 webServer 服务，声明后 Cordis 会等它出现再 apply"。所以 `inject: ['timer','webServer']`，路由才注册得上。
- **client 格式**：`window.__ModuleLoader__.load({ id, factory(require) })`，`require('react')` 外部化，纯 `React.createElement`。
- **host 格式**：CommonJS，`module.exports = { name, inject, apply }`。

---

## 5. 实测命令与已知事实

**面板服务（外部启动，Host 沙箱起不了）：**

```bash
cd /Users/a1/Workspace/WORKSPACE/visual_computer_operate
/tmp/vco-install-demo/venv/bin/python -m vco.cli debug \
  --port 8919 --profile "$PWD/.vco-runtime/profile" \
  http://127.0.0.1:3080/        # ← 这个 target 不能省
```

**为什么必须用那个 venv 的 python**：只有它装了 playwright。系统 `python3`（3.11）没有 → `ModuleNotFoundError: ModuleNotFoundError: No module named 'playwright'`。

**`~/.vco` 写不了**：默认 profile 目录会 `PermissionError`，所以用 `--profile` 指向工作区内。

**如何以已认证身份打开 dsh GUI 做实测**（我用这个抓到了所有关键证据）：

```python
from vco.debug_session import dsh_cookie_for, load_dsh_secret
secret = load_dsh_secret()
cookie = dsh_cookie_for("http://127.0.0.1:3080/", secret)   # 返回 {name, value}
# 塞进 Playwright context.add_cookies 后即可访问 3080（否则 401）
# 会话要点侧栏里那条才有标签栏：空白会话时 hideChrome，整个 tablist 都不渲染
```

**截图**（模型读不了图，但用户可以看）：
- `.vco-runtime/shots/panel-full.png` — 面板整页，标题栏 `http://127.0.0.1:3080/ · 已连上`
- `.vco-runtime/shots/panel-embedded.png` — 嵌在 iframe 里的形态（跨源 iframe 加载完好，1000×700，无 X-Frame-Options 拦截）

---

## 6. 文件清单

| 路径 | 状态 |
|---|---|
| `vco/debug_assets/panel.html` | 已改（精简侧边栏 + 浮动详情块样式 + `data-vco-panel` 身份标记） |
| `vco/debug_assets/panel.js` | 已改（`renderDetail` / `focusNewestPick` / `forwardToHost` / insert 按钮） |
| `tests/verify_panel_dom.py` | 16 项 DOM 断言，**全过** |
| `tests/verify_composer_insert.py` | ⚠️ **只是骨架，没真跑**（需要 dsh 页面模块图），要么补完要么删 |
| `dsh-plugin-vco-debug/` | 合规 bundle，**未安装** |
| `tests/e2e_fixtures/form.html` | 上一轮遗留的 fixture，用途不明，可删 |

**未提交**：以上全部还是工作区改动（`git status` 可见），没 commit。

---

## 7. 一句话总结

面板本身（精简、浮块、insert）**做完并验证过了**；把它作为**与 chat 同级的视图、排在 cdd-lens 后面**这件事，动态插件路径被守卫的负 priority 和启动时快照锁死，**必须走 bundle**——bundle 已写好，卡在 `~/.dsh` 写权限，需要用户跑三条命令然后重启 `dsh web`。

---

## 8. 续（后续一次）：插入链路修好并实测通过

**bundle 已经装上了**：`~/.dsh/profiles/web/node_modules/dsh-plugin-vco-debug` → 本目录，`package.json` 的 `dsh.profile.bundles` 里也有它。
实测 `conversation.view` occupants = `chat(0) · trajectory(10) · cdd-lens(20) · vco-debug(30)`，DOM `tabs = ['Chat','Trajectory','CDD Lens','VCO Debug']`。
**第 3 节那三条命令不用再跑了。**

**改之前实测到的两个真问题**（Playwright + dsh cookie 实测，不是推断）：

| 现象 | 证据 |
|---|---|
| `annotation` 根本没过河 | 面板 `postMessage` 只有 `{source,kind,text,selector,insert}`；client.js 只读 `kind/text/selector`。备注只是在「全部信息」的正文里**碰巧**被带上，其余三个按钮完全没有它 |
| 插入会**冲掉**已经写好的草稿 | 实测：输入框先打「我原本写的话」→ 点「插入输入框」→ 输入框只剩 payload。因为 `InputActions.setDraft` 的语义是 *replace the whole draft*（`ui-conversation/src/client/contract/input.ts:223`），这个 face 里没有 append 动词 |

**改法（两个文件）**：

1. `vco/debug_assets/panel.js` → `forwardToHost()` 的 payload 增加 `annotation` 字段（取 `detailNote || pick.annotation`）。
2. `dsh-plugin-vco-debug/src/client.js`：
   - 收下 `annotation`；footer 增加一个可编辑的 `input.anno`，面板填的备注预填进来、可以改；
   - 组装「annotation + DOM 信息」，正文已经以这条备注开头时不重复相加；
   - 插入改成**追加**：用 `useInput` 读当前 draft，`setDraft(existing + "\n\n" + payload)`；已经含这段就跳过不重复插；
   - 「插入并发送」把 `submit()` 挪进 `ctx.timeout(..., 0)`，不再和 draft 写入抢同一 tick。

**验证**：

- `tests/verify_panel_dom.py` **16/16 仍全过**，payload 里现在多出 `"annotation": ""` 字段。
- 端到端探针：`/Users/a1/Workspace/WORKSPACE/DeepSeek-Harness-provider/tests/probe-vco-insert.js`（带 dsh cookie 无头登录 → 进会话 → 切 VCO Debug → 注入面板消息 → 点按钮 → 读输入框）。实测：

```
[1] 插入前: "我原本写的话"
[1] footer: {"anno":"点了没反应", "buttons":["插入输入框","插入并发送","忽略"]}
[1] 插入后: "我原本写的话 … 点了没反应 - 选择器: `#submit.primary` …"   ← 追加，且备注没被写两遍
[2] footer: {"anno":"这个 + 号点了没反应"}
[2] 插入后: "这个 + 号点了没反应 … button.pp-plus"                    ← 选择器级载荷也带上了备注
errors: []
```

**生效方式**：面板服务是**按请求读盘**（`render_panel_js()`，路由 `/static/panel.js`），所以 `panel.js` 改完刷新 iframe 即可；`client.js` 是 boot bundle，刷新 DSH 页面即可（实测新开的页面跑的就是新代码，不用重启 `dsh web`）。
