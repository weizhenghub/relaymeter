# 支持图片的模型 / OpenCode 发图修复（v0.188）开发文档

> 中继端新增一个**全局**「支持图片的模型」多选：勾选的模型在 `/models/api.json`
> 里以 `modalities.input=["text","image","pdf"]` 出现，OpenCode 据此放行图片。
> 修复 OpenCode 通过中继发图报 `Cannot read "image.png" (this model does not
> support image input)`。

---

## 1. 用户的初始指令

> 小伙子，其实有源码：`C:\Users\weizheng\Downloads\opencode-dev.zip`

> 不是opencode，我的预期是在中继端做插件。你先不要做，先看看现在中继的插件承载逻辑。

> 我的设计是，可以在已有的模型列表里多选哪些模型支持图。

（初始曾尝试过 OpenCode 客户端插件方向，用户明确否定 —— 要求在中继端做；
经核对插件承载逻辑后，改用**写死**方式直接改 `routers/models.py`，全程零插件代码。）

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | **中继端**解决，不是 OpenCode 客户端插件 | "我的预期是在中继端做插件" |
| B | **全局模型名多选**，不区分上游 | "可以在已有的模型列表里多选哪些模型支持图" |
| C | **只改 `/models/api.json`**，不动中继转发层 | "只改 /models/api.json" |
| D | **不预置，全手动**，初始为空清单 | "不加预置，全手动" |
| E | 持久化到 upstreams.json 顶层 | 选定全局模型清单方案 |
| F | 一个上游可产出**多个**模型 entry | 设计决定（见 3） |

### 隐含但需要自行决策的点

- **粒度选「裸模型名」而非「上游\0模型」**：OpenCode 认的是模型 `id`
  （api.json 里的模型名），而模型名是**透传的、跨上游一致** ——
  `deepseek-v4-flash-vision-exp` 不管走哪个上游，在 OpenCode 里都是这个名字。
  所以按裸模型名全局匹配是正确粒度。
- **同名模型跨上游被同时标记**：全局按名匹配必然如此（如 MiniMax-M3 在两个
  上游同时出现则同时标记）。用户已接受此权衡（选「全局模型清单」）。
- **消费点是否在插件系统之外**：`/models/api.json` 是独立 FastAPI 路由，
  完全在插件系统之外（`_HOOK_NAMES` 里没有 "models"，无 hook/override 挂它）。
  所以改造它**不需要**过插件注册，直接写死即可。

---

## 3. 分析需求后得出的开发路径

### ⚠ 关键事实（从 OpenCode 源码确认，`packages/opencode`）

- **根因**：OpenCode 发请求前，`packages/opencode/src/provider/transform.ts:435`
  检查 `model.capabilities.input[modality]`。为假 → **在客户端本地**就把图剥掉
  + 报错，请求根本不会发到中继。中继 proxy 层**不剥图**（只剥 thinking/cache
  块），真正的拦点在 OpenCode 客户端。
- **capabilities 来源**：`capabilities.input.image`（`provider.ts:1468`）由
  `model.modalities?.input?.includes("image")` 决定。`modalities` 来自 OpenCode
  拉的 `${OPENCODE_MODELS_URL}/api.json`。
- **充分条件成立**：`OPENCODE_MODELS_URL=http://127.0.0.1:8088/models`，OpenCode
  拉的就是中继的 `GET /models/api.json`（`src/relay/routers/models.py`）。且
  `provider.ts:1433-1468` 合并逻辑会让 api.json 的 `existingModel.capabilities`
  补进 opencode.jsonc 手写的 relay provider —— **改 api.json 是充分条件**。
- **模型加载节奏（`packages/core/src/models-dev.ts`）**：模型目录不是每次请求都
  查。`source` 非默认时缓存到 `models-<hash>.json`；启动时 + 每 60 分钟
  `refresh()`；磁盘缓存 TTL 5 分钟；内存 `get()` 无限期缓存直到 refresh 成功才
  invalidate。**所以中继勾选后，OpenCode 要重启 TUI（或等 60 分钟刷新）才拉到新
  modalities**——不是中继要重启，是 OpenCode 要重启。

### 实现路径（完全镜像 `error_analysis` 的顶层配置链路）

```
#7  config.py: Settings 加 vision_models 字段 + save_vision_models   (底层)
#8  upstreams_file.py: apply_to_settings 读顶层 vision_models        (读配置)
#9  gui.py: get_vision_models / set_vision_models bridge + 快照字段  (依赖 #7)
#10 app.js: bridge + renderSettingsVision 多选 UI + dispatch        (依赖 #9)
#11 index.html: 新设置卡 + CSS/JS stamp bump                        (依赖 #10)
#12 models.py: _model_entry 按 vision_models 定 modalities（消费点）  (独立)
```

### 技术关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 数据存哪 | upstreams.json 顶层 `vision_models` | 与 error_analysis 同款，免 .env 扁平约束 |
| 勾选粒度 | 裸模型名（跨上游全局） | OpenCode 认模型 id，模型名跨上游一致 |
| modalities 常量 | `_IMAGE_MODALITIES` / `_TEXT_MODALITIES` 双常量按需选 | 不再统一拷贝 `MODALITIES`，避免污染共享常量 |
| 一上游产多 entry | 遍历 `cfg.model + allowed_models` 去重 | 让 `deepseek-v4-flash-vision-exp` 单独出现 |
| 去掉 thinking_options 过滤 | 是（这行是排除非 thinking 上游多模态模型的根因） | 否则用户勾不着的模型根本进不了清单 |
| fail-open 读配置 | 非 list 落默认空列表 | 配置损坏绝不阻塞整个 load |

---

## 4. 实现中遇到的问题

### 问题 1：`_model_entry` 原本统一拷贝 `MODALITIES`，无法按模型区分

原实现 `"modalities": dict(MODALITIES)`，所有出现的模型一律标 image。改造成按
`support_image` 分支后，**不能再从共享常量拷贝**（`dict(...)` 是浅拷贝、且
`MODALITIES` 是模块级共享对象）。改成在 `_model_entry` 里按 `support_image` 现场
构建 dict：`dict(_IMAGE_MODALITIES if support_image else _TEXT_MODALITIES)`。

### 问题 2：`if not cfg.thinking_options: continue` 是排除多模态模型的根因

`models_api` 原来只暴露声明了 `thinking_options` 的上游模型。`dp官方/
deepseek-vision-exp` 这类走非 anthropic-thinking 上游的多模态模型**根本不在**
api.json 里，OpenCode 连它的 modalities 都收不到 → 默认不能收图。去掉这行过滤。

### 问题 3：一个上游只产出 `cfg.model` 一个 entry

原循环对每个 cfg 只建一个 entry（id = `cfg.model or allowed_models[0]`）。
`deepseek-v4-flash-vision-exp` 若在 `allowed_models` 里而不是 `cfg.model`，就
被 `allowed_models[0]` 的前置模型盖掉。改成遍历 `[cfg.model, *allowed_models]`
去重，各自以模型名为 key 建 entry。

### 问题 4：模型名精确匹配链路核对

`get_vision_models` 返回的模型名清单是从 `_upstream_model_catalog(settings)`
抽取的 `m["model"]`（裸模型名）。`_model_entry` 算出的 `model_id` 取
`cfg.model or allowed_models[0] or cfg.name`；遍历模型名时用同一个 `model_id`
链判 `support_image = model_id in vision`。二者都取自 `cfg.model` /
`cfg.allowed_models` 元素 → 天然一致。

### 问题 5：OpenCode 拉取节奏（非 bug，但要告知用户）

中继侧 `models_api` 每次被请求时现读 `settings.vision_models` 是「活的」；但
OpenCode 侧模型目录有缓存（见 3 的「模型加载节奏」）。勾选后要 OpenCode 重启
TUI 才拉到新 modalities。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 / 行号 |
|---|---|---|
| 按模型定 modalities | `_model_entry(cfg, *, support_image)` 现场构建 dict | models.py:109-133 |
| 排除多模态的过滤 | 去掉 `if not cfg.thinking_options: continue` | models.py:148-149 |
| 一上游多 entry | 遍历 `cfg.model + allowed_models` 去重建 entry | models.py:153-165 |
| Settings 字段 | `vision_models: list[str] = []` | config.py:605 |
| 持久化 | `save_vision_models`（strip + 去重）写 `data["vision_models"]` | config.py:1429 |
| 读配置 | `apply_to_settings` 读顶层，fail-open | upstreams_file.py:556-561 |
| GUI bridge | `get_vision_models` / `set_vision_models` | gui.py:1697/1711 |
| 快照初值 | `"vision_models"` 列表供首帧 | gui.py:3175 |
| JS bridge | `getVisionModels` / `setVisionModels` | app.js:147-148 |
| 设置页 UI | `renderSettingsVision` 多选 chip + 保存 | app.js:9718-9785 |
| dispatch | 设置路由加 `renderSettingsVision(...)` | app.js:2868 |
| 设置卡 HTML | `card-settings-vision-body` | index.html:415-424 |
| chip 样式 | `.cfg-vm-chip` / `.cfg-vm-chip.active` | styles-20260817.css:4458-4464 |

---

## 6. 是否完全遵循规划路径开发

**完全遵循**。

- 用户三处拍板（全局模型清单 / 只改 api.json / 不加预置全手动）都严格实现。
- 镜像 `error_analysis` 顶层配置链路（Settings → GUI bridge → 设置卡 →
  upstreams.json）逐环节对应，无偏离。
- **全程零插件代码**：`routers/models.py` / `config.py` / `upstreams_file.py` /
  `gui.py` 均无 `def apply(ctx)` / `register_hook` / 挂 hook —— 就是写死逻辑。
  这是用户明确确认的点（「如果百分百确定是不会影响其它平台的，就不必做成
  插件了，直接写死即可」）。

### 没预测到的实际调整

- **(a) 快照字段要不要加 `vision_models`**：规划里标注「可选」。实现了，因为
  `renderSettingsVision` 守卫只渲染一次、需要首帧初值填 chip 勾选态，避免异步
  `getVisionModels` 期间首帧空白。与 `error_analysis_enabled` 那几行同款。
- **(b) 模型名清单要不要进快照**：不加。模型清单过大不进 snapshot（按需异步
  `get_vision_models` 拉），与 error_analysis 的「模型清单过大不进 snapshot」注释
  一致。

---

## 7. 最终实现点

### 后端（Python）

1. **`config.py`**（Settings 类，v0.188）：
   ```python
   # 存裸模型名（OpenCode 认模型 id），不区分上游。
   vision_models: list[str] = []
   ```
2. **`config.py` `save_vision_models(settings, payload)`**：
   - payload 为 `list[str]`（模型名），strip + 去空 + 去重
   - `_mutate(data)`：`data["vision_models"] = clean`
   - `return save_upstreams_json(settings, _mutate)`
3. **`upstreams_file.py` `apply_to_settings`**：读顶层 `vision_models`，fail-open
   （非 list 落默认空列表）。
4. **`gui.py` `get_vision_models`**：返回
   `{"models": [去重模型名], "vision_models": [当前勾选]}`，模型名清单从
   `_upstream_model_catalog` 抽取 `m["model"]` 去重排序。
5. **`gui.py` `set_vision_models`**：`save_vision_models` → `reload_settings` →
   返回 `{ok, path}`；reload 失败返回 `{ok: True, warning}`。
6. **`gui.py` 快照**：加 `"vision_models"` 列表供首帧。
7. **`routers/models.py` `models_api`**（消费点）：
   - 去掉 `if not cfg.thinking_options: continue`
   - `vision = set(settings.vision_models or [])`
   - 遍历 `settings.upstreams_for("anthropic")`（实际返回所有上游），每个 cfg 的
     `cands = [cfg.model, *allowed_models]` 去重，没有则 `[cfg.name or "auto"]`
   - 每个 `model_id`：`support_image = model_id in vision` →
     `_model_entry(cfg, support_image=support_image)`，`entry["id"] = model_id`，
     写 `relay_models[model_id]`
8. **`routers/models.py` `_model_entry(cfg, *, support_image)`**：modalities 按
   `support_image` 选 `_IMAGE_MODALITIES`（text/image/pdf）或 `_TEXT_MODALITIES`
   （纯 text），现场构建 dict。
   ```python
   _IMAGE_MODALITIES = {"input": ["text", "image", "pdf"], "output": ["text"]}
   _TEXT_MODALITIES  = {"input": ["text"], "output": ["text"]}
   ```

### 前端

9. **`app.js` bridge**（Api 对象内）：
   ```js
   getVisionModels()  { return this._call("get_vision_models"); },
   setVisionModels(p) { return this._call("set_vision_models", [p]); },
   ```
10. **`app.js` `renderSettingsVision(body, snap)`**：多选 chip 列表。
    - 先 `snap.vision_models` 同步填初值，再异步 `getVisionModels` 拉权威值
    - 所有 `data.models` 渲染成 `.cfg-vm-chip`，命中勾选集的加 `active` 类
    - 点击 chip toggle `active`；「保存」按钮 → `onSaveVisionModels` →
      `setVisionModels(选中的 model 名)` → `alertModal` 提示
    - 图片 hint 文案：勾选后 OpenCode 才允许向该模型发送图片
11. **`app.js` dispatch**：设置路由 `renderSettingsVision($("card-settings-vision-body"), snap)`
    （在 `renderSettingsError` 之后）。
12. **`index.html`**：`settings-error` 卡片后加
    `card-settings-vision-body` 设置卡（`data-card="settings-vision"`）。
13. **`styles-20260817.css`**：`.cfg-vm-chip`（cursor:pointer、去 remove 钮 padding）+
    `.cfg-vm-chip.active`（accent 描边高亮勾选态）。

### 资源文件版本

14. `index.html`：CSS `?v=20260826-10` → `-11`；JS `?v=20260826-11` → `-12`。

### 行为验收清单（自动 + 手动）

- [x] `save_vision_models(settings, [...])` → upstreams.json 顶层出现
  `vision_models`（strip + 去重生效）
- [x] `apply_to_settings` 从临时 upstreams.json 读回 `settings.vision_models`
- [x] `models_api` 消费逻辑：勾选的
  `deepseek-v4-flash-vision-exp` → `input=['text','image','pdf']`；
  未勾选的 `deepseek-v4-flash` → `input=['text']` 且**确实出现在** relay provider；
  `claude-opus-5` 保留 `reasoning=True`
- [x] `models_api` 路由（stub network）注入 relay provider，api=anthropic、npm=@ai-sdk/anthropic
- [x] Python AST 语法通过；`node --check app.js` 通过
- [ ] GUI 重启后（**严禁 kill 8088**，用户点 GUI 重启）→ 设置页出现「支持图片
  的模型」卡
- [ ] 勾选 `deepseek-v4-flash-vision-exp` → 保存
- [ ] OpenCode 重启 TUI → 用 `relay/deepseek-v4-flash-vision-exp` 发图不再报
  `does not support image input`
- [ ] 回归：未勾选的纯文本模型发图仍被 OpenCode 客户端拦截（预期，因没勾）

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/config.py` | 改（+约 20 行：Settings `vision_models` 字段 + `save_vision_models`） |
| `src/relay/upstreams_file.py` | 改（+约 8 行：`apply_to_settings` 读顶层 `vision_models`） |
| `src/relay/gui.py` | 改（+约 40 行：`get_vision_models`/`set_vision_models` bridge + 快照字段） |
| `src/relay/routers/models.py` | 改（重写 `models_api` 循环 + `_model_entry` 加 `support_image`；`MODALITIES` → `_IMAGE_MODALITIES`/`_TEXT_MODALITIES`） |
| `src/relay/web/app.js` | 改（bridge + `renderSettingsVision`/`onSaveVisionModels` + dispatch） |
| `src/relay/web/index.html` | 改（`card-settings-vision-body` 设置卡 + CSS/JS stamp bump `-10/-11` → `-11/-12`） |
| `src/relay/web/styles-20260817.css` | 改（`.cfg-vm-chip` / `.cfg-vm-chip.active`） |
| `docs/dev/vision_models_opencode_image_v0.188.md` | 新增（本文件，7 节） |
| `docs/CHANGELOG.txt` | 新增 v0.188 条目 |

---

### ⚠ 重要约束（写入本需求必须记住的）

- **OpenCode 侧有缓存**：勾选保存后，中继侧立即生效，但 OpenCode 要**重启 TUI**
  （或等 60 分钟定时刷新）才拉到新 `modalities`。不是中继需要重启。
- **空名单回归预期**：`vision_models` 初始为空，升级后 `claude-opus-5`/`fable-5`
  等回到纯文本（OpenCode 会拦图），需手动勾回。这是用户拍板「全手动」的
  一次性代价，知情并接受。
- **模型名精确匹配**：`_model_entry` 的 `model_id` 必须与 `get_vision_models`
  返回的模型名**精确相等**才命中。二者同源（都取 `cfg.model` /
  `cfg.allowed_models` 元素），天然对齐。
