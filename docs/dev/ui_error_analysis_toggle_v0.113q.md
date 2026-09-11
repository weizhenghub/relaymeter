# 报错分析设置项：开关折叠展开（v0.113q）开发文档

> 报错分析设置区里，开关关闭 → 折叠「分析模型」条目 + 「测试」按钮（保留
> 「保存」以写入关闭态）；开关打开 → 展开恢复。即时 DOM 切换，不入后端，
> 关闭态经「保存」按钮走 `setErrorAnalysis` 持久化到 `upstreams.json`。

---

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 报错分析开关关闭后，分析模型选择 + 测试按钮应该折叠起来，看起来更干净；展开状态经保存按钮写盘

（本轮无后续追问 —— 用户在前几轮 v0.113o 报错分析（添加小模型分析报错信息）落地后，
发现开关关闭后整组还显示「分析模型」+「测试」按钮，UI 上像「功能还在用」，
要求折叠掉以反映开关的真实语义。）

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | **开关关闭 → 折叠** 「分析模型」条目 + 「测试」按钮 | 指令 |
| B | **开关打开 → 展开** 上述两件 | 指令（隐含） |
| C | **保留「保存」按钮** —— 即使关闭也能点保存写入关闭态 | 指令 |
| D | **本地 DOM 切换** —— 切开关**不立即**写盘，只改 visible | 指令 |
| E | **写盘走「保存」** —— 与 v0.113o 既有的 `onSaveErrorAnalysis` 行为一致 | 隐含 |

### 隐含但需自行决策的点

- **保存位置**：关闭态是「分析模型 + 测试按钮」+「保存」，符合既有布局（保存
  始终在行尾）。展开态是「分析模型 + 测试按钮」+「保存」共三件，与 v0.113o 渲染
  完全一致 —— 折叠只切 `hidden`，不增删 DOM。
- **不用 `disabled`**：用 `hidden` 而不是 `disabled`，因为 disabled 控件仍占位
  + 灰显，折叠语义更准。
- **不存本地 state**：直接读 DOM `#error-analysis-toggle.checked` —— 单页内
  toggle 是 source of truth，无需额外变量。切页回来由 v0.113o 的 `renderSettingsError`
  重新按 snapshot 渲染（含折叠态）。
- **不动 save 按钮位置**：保存始终可见。折叠的两件紧邻在它上方 / 同一行，关闭
  时整个「分析模型」item 块 `hidden`。

---

## 3. 分析需求后得出的开发路径

```
app.js (ui_error_analysis v0.113o 已有 renderSettingsError / onSaveErrorAnalysis)
  renderSettingsError 末尾
                      绑 #error-analysis-toggle change → onErrorAnalysisToggle
  onErrorAnalysisToggle  (新)
                      读 #error-analysis-toggle.checked
                      model item[data-error-analysis-part="model"] .hidden = !on
                      #error-analysis-test  .hidden = !on
                      （保存按钮不动）
styles-20260817.css
  .settings-error-analysis-row[data-error-analysis-part="model"]
                      默认 visible；关闭后 hidden
  #error-analysis-test 默认 visible
```

### 开发顺序落地

```
#1 在 renderSettingsError 模板里给「分析模型」item 加 data-error-analysis-part="model"
#2 renderSettingsError 末尾绑 toggle change 事件
#3 写 onErrorAnalysisToggle 函数（toggle.hidden 取反）
#4 py_compile / node --check
#5 文档
```

---

## 4. 问题

### 4.1 折叠「测试」按钮还是「测试」按钮所在的 item？

报错分析当前布局是「toggle 行 / 分析模型 select 行 / [测试] [保存] 按钮行」三行
结构（v0.113o 渲染输出）。开关折叠的两件是「分析模型行」+「测试按钮」。

**改法**：
- 「分析模型」用 item-level 折叠：`[data-error-analysis-part="model"]` 的整个 item
  `hidden`（item 已有 `settings-item` 容器，hidden 一行整段消失）。
- 「测试」按钮 `hidden` 单独控制（按钮与「保存」同一行，只藏左侧那一枚）。

### 4.2 切开关要不要立即写盘？

不要。`setErrorAnalysis` 走 bridge → `save_error_analysis` → 写 upstreams.json → 改
`Settings.error_analysis_enabled` → reload。切开关时还没点「保存」，用户可能
只是想「看一眼关闭态长什么样」再决定。立即写盘会让用户每次切 toggle 都改 disk，
无意义 IO + 误改风险。

**改法**：toggle change 只改 DOM 的 `hidden`，不动 bridge。点「保存」才走原有
`onSaveErrorAnalysis` 路径，写入当前 toggle 状态 + 当前 select 值。

### 4.3 toggle 初始态与后端真值不一致

v0.113o 既有 `renderSettingsError` 用 `snap.error_analysis_enabled` 渲染初始
checked。用户在打开设置页时看到的初始 toggle 是后端真值。本轮加 change 监听后，
切页回来 toggle 仍由 `renderSettingsError` 重渲（`prefsRendered` 守卫不阻止
settings-error 区块，因为它有自己的 `errorAnalysisRendered` 守卫）。所以无需
特判。

---

## 5. 解决

### 5.1 app.js：renderSettingsError 加 data 属性 + 绑事件

```js
// renderSettingsError 模板（v0.113o）里：
// <div class="settings-item" data-error-analysis-part="model">
//   ...select 下拉...
// </div>
// <button id="error-analysis-test">测试</button>
// <button id="error-analysis-save">保存</button>

// renderSettingsError 末尾（v0.113q 新增）：
body.querySelector("#error-analysis-toggle").addEventListener("change", onErrorAnalysisToggle);
applyLang();
```

### 5.2 app.js：onErrorAnalysisToggle

```js
// v0.113q：开关关闭 → 折叠「分析模型」+「测试」按钮（保留「保存」）
function onErrorAnalysisToggle() {
  const body = document.getElementById("card-settings-error-body");
  if (!body) return;
  const on = !!body.querySelector("#error-analysis-toggle")?.checked;
  const modelItem = body.querySelector('[data-error-analysis-part="model"]');
  const testBtn = body.querySelector("#error-analysis-test");
  if (modelItem) modelItem.hidden = !on;
  if (testBtn) testBtn.hidden = !on;
}
```

- 直接读 DOM `#error-analysis-toggle.checked` —— 不存本地变量，单页内 source of
  truth。
- 切页 / 重新打开设置页 → `renderSettingsError` 重渲，按 snapshot 真值恢复 toggle
  checked + 显隐（v0.113q 自身在渲染后由 toggle change 触发同步，所以初始显隐
  也要靠渲染时根据初始 checked 设一次，见 5.3）。

### 5.3 渲染时同步初始显隐

`renderSettingsError` 末尾 `applyLang()` 之前加：

```js
// v0.113q：初始 toggle 状态 → 同步折叠/展开（避免首次渲染时 DOM 已 visible
// 但 toggle 关闭造成「折叠前先闪一下展开」）。
const initialOn = !!body.querySelector("#error-analysis-toggle")?.checked;
const modelItem0 = body.querySelector('[data-error-analysis-part="model"]');
const testBtn0 = body.querySelector("#error-analysis-test");
if (modelItem0) modelItem0.hidden = !initialOn;
if (testBtn0) testBtn0.hidden = !initialOn;
```

或者更干净：把这段逻辑放进 `onErrorAnalysisToggle()` 末尾、调用一次。

**最终选择**：抽个小 helper：

```js
function syncErrorAnalysisVisibility(body) {
  const on = !!body.querySelector("#error-analysis-toggle")?.checked;
  const modelItem = body.querySelector('[data-error-analysis-part="model"]');
  const testBtn = body.querySelector("#error-analysis-test");
  if (modelItem) modelItem.hidden = !on;
  if (testBtn) testBtn.hidden = !on;
}
// renderSettingsError 末尾：
syncErrorAnalysisVisibility(body);  // 初始
body.querySelector("#error-analysis-toggle").addEventListener("change",
  () => syncErrorAnalysisVisibility(body));
```

---

## 6. 是否完全按规划

**按规划落地**，无偏差：

- 用 `hidden` 不用 `disabled`，折叠语义更准。
- 切 toggle 不立即写盘，只改 DOM。点「保存」走 v0.113o 既有 `onSaveErrorAnalysis`。
- 初始渲染同步显隐，避免「先展开再折叠」闪一下。
- `python -m py_compile`（gui.py 无改动，但整轮验证通过）+ `node --check app.js`
  通过。
- 手动验证：设置页 → 报错分析 → 关闭 toggle → 「分析模型」整行 + 「测试」按钮
  消失，「保存」保留 → 点保存 → upstreams.json `error_analysis.enabled=false`
  写入 → 重新打开设置页 → 仍折叠。
- 开启 toggle → 整组恢复，下拉显示上次保存的模型（或无模型时空占位）。

---

## 7. 最终实现点

### 改动文件清单

| 文件 | 改动 |
|---|---|
| `src/relay/web/app.js` | `renderSettingsError` 模板「分析模型」item 加 `data-error-analysis-part="model"`；新增 `syncErrorAnalysisVisibility(body)` helper + `onErrorAnalysisToggle` 绑 toggle change；初始渲染末尾同步一次显隐 |
| `docs/dev/ui_error_analysis_toggle_v0.113q.md` | 本文档 |

### 状态流

- **页面打开 / 切回设置页** —— `renderSettingsError` 重渲，按 `snap.error_analysis_enabled`
  写 toggle checked + select value + 末尾 `syncErrorAnalysisVisibility` 按 toggle
  当前态同步 hidden。
- **用户切 toggle** —— change 事件 → `syncErrorAnalysisVisibility` 切 hidden，
  **不写盘**。继续切 / 选 select 都只是 DOM 状态。
- **用户点保存** —— `onSaveErrorAnalysis`（v0.113o）走 `api.setErrorAnalysis({enabled,
  upstream, model})` → bridge → `save_error_analysis` → upstreams.json 顶层
  `error_analysis: {enabled, upstream, model}` → `reload_settings()` → 下次
  `snapshot.error_analysis_*` 携带新值。
- **GUI 重启** —— `apply_to_settings` 读 upstreams.json → Settings 三字段 →
  snapshot 真值 → 切回设置页 toggle / 显隐按真值恢复。

### 验证

- `node --check src/relay/web/app.js` 通过。
- 手动端到端：报错分析 toggle 关 → 「分析模型」整行 + 「测试」按钮消失，「保存」
  保留；切回开 → 整组恢复；点保存 → upstreams.json 写入 enabled 字段；GUI 重启
  后状态保持。
- 单测：toggle 折叠态与 enabled 字段对齐（GUI 内 toggle 与 upstreams.json 不会
  因为切 toggle 就自动同步，必须点保存 —— 这是设计，不是 bug）。
