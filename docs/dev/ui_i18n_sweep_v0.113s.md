# 全站 i18n 第二轮扫荡：实时栏管理 + 报错分析 + quota block（v0.113s）开发文档

> v0.113r 把「总览/历史/统计」主路可见文案 + nav-sub 默认隐藏项 + flash 弹窗编辑器全部 i18n 化
> 后，用户继续报：「实时栏管理、允许小model分发报错信息，一用xxx次、已用xx%未能转换」——
> 翻译器（user-facing 文本）翻译时还存在三块盲区：
>
> 1. **实时栏管理整组**默认 hidden（依赖 `syncLivePanelMgmtGroup` 才显示），walker 跳过；
> 2. **报错分析设置组**位于设置页子菜单「报错分析」下，节点创建时未挂 `data-i18n`，靠 walker 兜底；
> 3. **quota block**（上游卡片的 5h 额度 / 释放时间 / 非允许模型）整段是 innerHTML 拼接的硬编码中文字符串，
>    `t()` 机制压根没参与。
>
> 另外扫荡时发现 **上游详情 meta 行**（5h/周/月/释放）也是同类问题，顺手一起修。
>
> 本次会话只补翻译，不动 relay 进程 / proxy 路径 / 任何 i18n 框架本身。

---

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 实时栏管理，允许小model分发报错信息，一用xxx次，已用xx%未能转换

（用户没追问；这是 v0.113r 完成后下一轮反馈。语义拆解：

- 「实时栏管理」= 设置页 → 界面与偏好 → 实时栏管理组（13 项中文）
- 「允许小model分发报错信息」= 设置页 → 报错分析组（5 项中文）
- 「一用xxx次」= 上游 quota 概要：「已用 X 次 / Y 次」
- 「已用xx%未能转换」= quota 进度条文案 + 释放时间）

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | **实时栏管理组** 13 项全部加 `data-i18n`，避免依赖 walker 命中（默认 hidden=true） | 用户反馈「实时栏管理未能转换」 |
| B | **报错分析组** 5 项全部加 `data-i18n`（含「测试」「保存」按钮） | 用户反馈「允许小model分发报错信息未能转换」 |
| C | **quota block** 硬编码字符串（「次」/「tokens」/「额度已用尽」/「预计 X 用尽」/「非允许模型」）包 `t()` | 用户反馈「一用xxx次、已用xx%未能转换」 |
| D | **上游详情 meta 行**「5h: / 周: / 月:」+「5h 释放 X」+「累计 tokens」改为 `t()` | 扫荡时同类发现 |
| E | **title 属性**「点击激活此上游…」「删除这条上游配置」改为 `t()` | 扫荡时同类发现 |
| F | **透传模式提示占位**（`<div class="pt-empty-note" hidden>`）3 项加 `data-i18n` | 探针显示其含中文且在 DOM 中 |
| G | **i18n 字典扩容**：~13 个新 key + 对应英文翻译 | A-F 项所需翻译来源 |
| H | **版本号 bump** + dev doc | 项目惯例 |

---

## 3. 需求分析 / 开发路径

### 3.1 为什么用 `data-i18n` 而不是依赖 walker

v0.113r 的 `applyLang()` 用 tree walker 扫 textNode，但有两个先天弱点：

1. **hidden 元素**：walker 在 `acceptNode` 里 walk up 父链遇到 `a.hidden` 就 `FILTER_REJECT`；
   实时栏管理组整组默认 `<div id="live-panel-mgmt-group" hidden>`，要等 `syncLivePanelMgmtGroup`
   把 `hidden` 撤掉才进入 walker 范围 —— 但那时是否还有别的 i18n 任务在排队是脆弱的。
2. **重复拼装**：renderer 重写时序组（设置切语言、点开子菜单）会触发 innerHTML 改写；
   walker 要在「隐藏→显示」的瞬间正好命中才翻译，否则一直中文。

`data-i18n` 元素级映射（v0.113r 已有的双轨机制）走独立路径：不看 hidden、不看 walker，只看
`el.__i18nZh` 缓存 + `I18N.en[key]`，**首次 mount 后就稳定翻译，setView/隐藏切换都不丢**。

### 3.2 quota block 为什么不能 data-i18n

`quotaBlock`（app.js:3117-3154）是函数返回的 innerHTML 字符串，含 5 个硬编码中文 token：

- `unit = data.billing_unit === "token" ? "tokens" : "次"` → 整段变量替换
- `${util >= 1 ? "额度已用尽" : \`预计 ${...} 用尽\`}` → 三元 + 模板字符串
- `⚠ 非允许模型:` → 字面量

每条只出现一次、且在循环结构里 —— 加 `data-i18n` 会需要拆分 DOM 重写，反而不如 `t()` 包裹源码 1 行字面量来得直接。

### 3.3 上游详情 meta 行同理

`upstream-detail-meta`（app.js:3327-3332）的 `<span>5h: <b>0</b></span>` 是
模板字符串字面量，没有独立 element 包标签；`WIN_LABEL[w] || w`（app.js:658）也是。两条都靠 `t()`。

### 3.4 风险点

- **「5h」key**在英文里也是 "5h"（无变化），但 `t()` 仍会查 dict 命中，**没命中回退原 key**——
  dict 里有 `"5h": "5h"` 时 zh→en 行为正确，zh→zh 等价。
- **透传模式提示占位**在 passthrough_mode=false 时 `hidden` 永远不撤 —— 加 `data-i18n` 后
  即使 walker 不命中，元素级映射也保证它正确翻译。
- **title 属性**：walker 不扫 attribute，只扫 textNode；title 不走 `data-i18n` 也不走 walker。
  唯一办法是把字面量在源码里包 `t()`（由 render 时把当前 lang 的译文注入到 template string）。

---

## 4. 问题（实施过程中遇到的坑）

| # | 问题 | 解决 |
|---|---|---|
| 1 | quota 字典 key 「次」太短，跟其它同名单字冲突 | 搜过 `I18N.en["次"]` / `"tokens"` / `"额度已用尽"` 全部不存在（先前只有 `"输入 tokens"` / `"输出 tokens"` 这类长 key），新增独立项即可 |
| 2 | `WIN_LABEL` 在 `renderUpstreamsView` 内部定义（local const），改 `t()` 不需要从外层注入 | 直接 `t("周")` / `t("月")` 替换字面量；其它同名 key 已存在（dict 后面会覆盖） |
| 3 | 「累计 tokens」字面量被夹在 template string 三元里 → 整段替换要保持缩进 | Read 拿到精确缩进后 Edit |
| 4 | 探针 walker 不扫 hidden 元素，但实际渲染里 hidden 元素仍出现在 DOM 里 | 探针不在乎；生产环境 walker 也不在乎——只是 data-i18n 兜底翻译 |
| 5 | 「透传模式不使用配置型上游…」的 key 在 dict 里是 `\"` 转义双引号；HTML 里实体是 `&quot;` | 用同一字面量字符即可 —— `t()` 不解析引号，只做整串替换 |
| 6 | 上游卡片 `title=` 属性同样需要翻译 | 改成 `${t("…")}` 插值即可（template string 上下文） |

---

## 5. 解决（最终方案）

### 5.1 实时栏管理组（live-panel-mgmt-group，app.js:4689-4757）

13 个元素加 `data-i18n`：

- `<div class="settings-group-label" data-i18n="实时栏管理">实时栏管理</div>`
- 6 个 `.settings-item-title`：允许并发显示 / 最多显示的数量 / 始终开启一个 / 思考流超时时间 / 思考-正文衔接超时时间 / 正文流超时时间
- 6 个 `.settings-item-hint` 配对

字典 key 在 v0.113n 已有，本次不增。

### 5.2 报错分析组（renderSettingsError，app.js:5839-5872）

7 个元素加 `data-i18n`：

- `<div class="settings-group-label" data-i18n="报错分析">报错分析</div>`
- `<div class="settings-item-title" data-i18n="允许小模型分析报错信息">允许小模型分析报错信息</div>`
- `<div class="settings-item-hint" data-i18n="出现报错时把错误信息发给所选模型…">…</div>`
- `<div class="settings-item-title" data-i18n="分析模型">分析模型</div>`
- `<div class="settings-item-hint" data-i18n="建议选择轻量快速的小模型…">…</div>`
- `<div class="settings-item-hint" data-i18n="「测试」用一条示例 429 报错…">…</div>`
- `<button data-i18n="测试">测试</button>` + `<button data-i18n="保存">保存</button>`

新增 5 个字典项：

```js
"允许小模型分析报错信息": "Let a small model classify errors",
"出现报错时把错误信息发给所选模型，判断错误类型并给出中文提示（余额不足 / 网络错误 / 达到次数限制等）。": "On error, send the error info to the selected model to classify the type and produce a short Chinese hint (out of balance / network error / rate limit, etc.).",
"分析模型": "Analysis model",
"建议选择轻量快速的小模型；留空则使用默认上游的兜底模型。": "Pick a lightweight, fast small model; leave blank to use the default upstream fallback model.",
"「测试」用一条示例 429 报错真实跑一次分类，验证模型与提示词。保存后立即生效，无需重启中继。": "\"Test\" runs a real classification on a sample 429 error to verify the model and prompt. Saves take effect immediately, no relay restart needed.",
```

### 5.3 quota block（app.js:3117-3154）

4 个硬编码字符串包 `t()`：

```js
const unit = data.billing_unit === "token" ? t("tokens") : t("次");
<span class="quota-summary-eta">${util >= 1 ? t("额度已用尽") : `${t("预计")} ${escape(data.exhaustion_eta_text || "—")} ${t("用尽")}`}</span>
`<div class="quota-unauthorized">⚠ ${t("非允许模型")}: ${escape(unauthorized.join("、"))}</div>`
```

新增 6 个字典项：

```js
"次": "calls",
"tokens": "tokens",
"额度已用尽": "Quota exhausted",
"预计": "ETA ",
"用尽": "until exhaustion",
"非允许模型": "Disallowed models",
```

> 「预计」/「用尽」拆 key 是为翻译时语法灵活（中文拼接「预计 2h 用尽」/ 英文拆 "ETA 2h until exhaustion"）。

### 5.4 上游详情 meta 行（app.js:3327-3332）

3 行 `<span>` 把字面量「5h:」「周:」「月:」包 `t()`；新增 3 个字典项：

```js
"5h": "5h",
"周": "week",
"月": "month",
```

### 5.5 释放时间行

3 处（app.js:710 / 732 / 3331）`5h 释放 ${relText}` 包 `t("5h 释放")`；新增：

```js
"5h 释放": "5h release",
```

### 5.6 上游窗口 token 行（app.js:669）

「累计 tokens」字面量包 `t("累计 tokens")`；新增：

```js
"累计 tokens": "Total tokens",
```

### 5.7 卡片 title 属性（app.js:3270 / 3317）

`title="删除这条上游配置"` / `title="点击激活此上游；已是当前上游时点击进入编辑"` →

```js
title="${t("删除这条上游配置")}"
title="${t("点击激活此上游；已是当前上游时点击进入编辑")}"
```

新增 2 个字典项：

```js
"删除这条上游配置": "Delete this upstream config",
"点击激活此上游；已是当前上游时点击进入编辑": "Click to activate this upstream; click again to edit when it's already active",
```

### 5.8 透传模式提示占位（index.html:241-247）

3 个元素加 `data-i18n`：「上游」/「透传模式」/「透传模式不使用配置型上游…」。字典里已覆盖。

---

## 6. 是否完全按规划

✅ 是。

- 所有「实时栏管理」「报错分析」项都加了 `data-i18n`；
- quota block 4 个硬编码字符串全部包 `t()`；
- 上游详情 meta 行 + title 属性 + 释放时间一并补齐；
- 透传模式 hidden 块顺手 data-i18n；
- 字典 ~16 个新 key 全部带英文；
- 版本 bump 至 `app.js?v=20260822-27`；
- 探针两次：①settings 两组（live-panel-mgmt + error-analysis）全部 0 CJK；
  ②upstreams 页（quota block + 上游详情 meta 行）全部 0 可见 CJK。

唯一未做的：i18n 框架本身（v0.113r 已有），不重写。

---

## 7. 最终实现点（落地产物）

| 文件 | 改动 |
|---|---|
| `src/relay/web/app.js` | live-panel-mgmt-group 13 项 data-i18n + renderSettingsError 7 项 data-i18n + quotaBlock 4 处 t() + WIN_LABEL t() + 上游详情 meta 行 3 处 t() + 释放时间 3 处 t() + token 窗口 1 处 t() + title 2 处 t() + 字典 16 个新 key |
| `src/relay/web/index.html` | 透传模式提示占位 3 个元素 data-i18n + 版本号 `app.js?v=20260822-27` |
| `docs/dev/ui_i18n_sweep_v0.113s.md` | 本开发文档 |

### 验证

1. **设置页两个新组**：EN 模式打开「界面与偏好 → 实时栏管理」+「报错分析」，
   全部项（13 + 7）显示英文，无中文残留。
2. **上游页 quota block**：EN 模式打开「上游」详情卡（带配额的上游），quota 概要行
   「80 calls / 100」「ETA — until exhaustion」+ 非允许模型「⚠ Disallowed models: gpt-4」
   全部英文。
3. **上游详情 meta 行**：「5h: / week: / month:」+「5h release 2h」+「Total tokens」全部英文。
4. **title 提示**：hover 卡片显示英文。
5. **zh 切回**：所有位置正确还原中文。
6. **不影响其它**：用 pywebview api + 同样探针跑一遍 settings / overview / history / live panel，
   回归无变化。