# 移除内容框左侧细彩色条（v0.113j）开发文档

> 一次性 UI 收尾：全 app 内容框左侧的细彩色条（角色色条 / active 绿条）全部删掉。
> 用户明确这类「左侧指示条」设计今后不再需要。

---

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 所有内容框左侧的细彩色条删掉。以后不再需要此类设计

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 删除「内容框左侧的细彩色条」这一设计元素（全量） | 指令 1 |
| B | 该设计模式今后不再新增（约定，写入注释） | 指令 1 |

### 隐含但需自行决策的点

- **「内容框」指哪些**：不靠猜，headless 全页 DOM 扫描（真实样式 + stub bridge 渲染真实视图）
  量化哪类元素有「非灰、非 hairline 的左边框」或「左侧细竖条背景」，得出全量清单。
- **role 可辨性**：modal 消息的角色条删掉后，`.modal-msg-role` 文本标签仍在，角色仍可辨，
  无需补样式。
- **active 状态可辨性**：设置页 active 行的绿条删掉后保留淡绿底，active 状态仍可辨；
  否则丢状态提示。
- **其它左边框不动**：hairline 灰边（nav-sub / settings-item-nested / history 分隔等）是
  结构线不是「彩色条」，保留。

---

## 3. 分析需求后得出的开发路径

```
主线  定位全量「彩色左边条」→ 逐一删除 → headless 验证零残留
  styles-20260817.css  .modal-msg-* 角色色条（3px 橙/绿/灰）
                        .settings-upstream-row:has(.active-tag) active 绿条（2px 绿）
```

### 开发顺序落地

```
#1 全 app 搜索 border-left / ::before / ::after / 平台色变量，列出候选
#2 headless 真实渲染：全页 DOM 扫描「彩色左边框 / 左侧细竖条背景」→ 候选收敛
#3 确定全量清单：modal 消息气泡角色条 + 设置页 active 绿条（唯一两处）
#4 删除色条规则，保留必需的可辨性（role 文本 / active 淡绿底）
#5 headless 验证：modal 气泡 border-left 全部回落到 1px hairline，无残留
#6 版本 bump + node --check + 清理探针文件 + 写文档
```

---

## 4. 问题

### 4.1 靠 grep 猜不准：候选分散、语义不一

全 CSS/JS 有 20+ 处 border-left，但绝大多数是 hairline 灰边（结构线）或三角形折叠箭头，
不是「彩色条」。候选要逐条分辨：

| 位置 | 内容 | 是否「彩色左边条」 |
|---|---|---|
| `.modal-msg-user/assistant/thinking/other`（3595-3599） | 3px 实线，橙（--platform-anthropic）/ 绿（--platform-openai）/ accent / 灰 | **是**（角色装饰） |
| `.settings-upstream-row:has(.active-tag)`（2877/2879） | 2px 绿（--text-success） | **是**（active 指示） |
| `.nav-sub`（503）/ `.settings-item-nested`（2615） | 2px hairline | 否（结构线，灰） |
| `.cfg-help h2/h3`（4368/4376） | 2-3px border-soft | 否（灰分组线） |
| `.upstream-config > summary::before`（3177） | 5px currentColor | 否（折叠三角箭头） |
| `.modal-msg-thinking` 的 `var(--accent)` | **死代码**（--accent 未在 CSS 定义 → IACVT → 实际无条） | 否（顺手清理） |

### 4.2 headless 全页扫描确证：全 app 只有两处彩色左条

不靠猜，写探针页（真实样式表 + stub bridge 渲染 overview/history/upstreams/stats/settings/
config 各视图 + 打开请求详情 modal），遍历 `body *` 计算样式：

- **检测条件**：`border-left-width > 1px` 且颜色非灰（RGB 三通道 max−min > 20）；或
  `width ≤ 6px 且 height > 14px` 且背景为彩色的竖条元素。
- **结果**：各视图 0 命中；**modal 消息气泡命中 3 条**（user 橙 / assistant 绿 / other 灰）。
- 结合 4.1 的静态清单，设置页 active 绿条是唯一未被扫描覆盖的（设置上游列表需更多 prefs
  API），静态确认后一并删除。

---

## 5. 解决

### 5.1 styles-20260817.css：删除 modal 消息气泡角色色条

```css
/* 改前 */
.modal-msg-user       { border-left: 3px solid var(--platform-anthropic); }
.modal-msg-assistant  { border-left: 3px solid var(--platform-openai); }
.modal-msg-thinking   { border-left: 3px solid var(--accent); opacity: 0.85; }
.modal-msg-thinking .modal-msg-role { font-style: italic; }
.modal-msg-other      { border-left: 3px solid var(--text-muted); }

/* 改后 */
/* v0.113j：去掉消息框左侧的角色彩色条（此类左侧细条设计不再使用）。
   role 仍有文本标签（.modal-msg-role）可辨；thinking 仅保留淡显样式。 */
.modal-msg-thinking   { opacity: 0.85; }
.modal-msg-thinking .modal-msg-role { font-style: italic; }
```

- user / assistant / other 整条规则删除（3px 色条消失，气泡回落 `.modal-msg` 的
  `border: 1px solid var(--hairline)`）。
- thinking 的 `border-left: 3px solid var(--accent)` 是死代码（--accent 未定义 → 无效），
  一并清掉，只留 `opacity: 0.85` 淡显。

### 5.2 styles-20260817.css：删除设置页 active 绿条，保留淡绿底

```css
/* 改前 */
.settings-upstream-row {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 8px;
  border-left: 2px solid transparent;   /* 预留色条空间 */
}
.settings-upstream-row:has(.active-tag) { border-left-color: var(--text-success); background: rgba(16, 163, 127, .04); }

/* 改后 */
.settings-upstream-row {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 8px;
}
/* v0.113j：去掉 active 行左侧绿条（此类左侧细条设计不再使用），
   保留淡绿底作为 active 状态提示。 */
.settings-upstream-row:has(.active-tag) { background: rgba(16, 163, 127, .04); }
```

- 基础规则的 `border-left: 2px solid transparent`（只为给色条留位）一并删除。
- `:has(.active-tag)` 只留 `background` 淡绿，active 状态仍可辨。

### 5.3 headless 验证

探针渲染 modal 四种气泡，实测改后样式：

```
modal-msg-user       border-left=1px rgb(229, 229, 234)   ← 回落 hairline，色条消失
modal-msg-assistant  border-left=1px rgb(229, 229, 234)
modal-msg-thinking   border-left=1px rgb(229, 229, 234)
modal-msg-other      border-left=1px rgb(229, 229, 234)
```

改前：user 3px 橙(217,119,6) / assistant 3px 绿(16,163,127) / other 3px 灰。改后全部回落到
1px hairline。设置页 active 行色条静态确证删除（绿条规则不再存在）。

---

## 6. 是否完全按规划

**完全按规划，无偏差。**

- 全量定位只花了 headless 全页扫描一轮（真实渲染 + 计算样式），没有靠猜或只看静态代码
  就下结论 —— 这也符合「视觉反馈先看真实渲染」的既定规则。
- 只删「彩色左条」两处；hairline 结构线 / 折叠三角箭头 / 水平进度条一律保留，没有误伤。
- 可辨性保留：role 走文本标签，active 走淡绿底，删条不丢状态。
- zoom=1 无回归（纯样式删条，不涉及尺寸）。
- 版本号 bump：styles `?v=20260822-17`（index.html + live_panel.html）。app.js 零改动。

---

## 7. 最终实现点

### 改动文件清单

| 文件 | 改动 |
|---|---|
| `src/relay/web/styles-20260817.css` | 删 `.modal-msg-user/assistant/other` 3px 角色色条、`.modal-msg-thinking` 死 border-left；删 `.settings-upstream-row` 预留透明左边框与 `:has(.active-tag)` 的绿条色，保留淡绿底 |
| `src/relay/web/index.html` | 版本号 query bump：styles `?v=20260822-17` |
| `src/relay/web/live_panel.html` | 版本号 query bump：styles `?v=20260822-17`（共享样式表缓存） |

### 状态流

- **内容框色条（A）**：请求详情 modal 的消息气泡不再有橙/绿/灰角色条（回落 hairline 边框），
  角色由 `.modal-msg-role` 文本标签辨认。
- **active 绿条（B）**：设置页 active 上游行不再有绿条，active 状态由淡绿底提示。
- **设计约定（C）**：注释标注「此类左侧细条设计不再使用」，后续新增样式不再引入。

### 验证

- headless 渲染四种 modal 气泡：border-left 全部 1px hairline，无彩色残留。
- 全 app 扫描：无其它「彩色左边框 / 左侧细竖条背景」元素残留。
- `node --check app.js` 通过（app.js 未改动）。
- 刷新 GUI：打开任一请求详情 —— 消息框左侧无彩色条；设置页上游列表 —— active 行无绿条，
  淡绿底保留。
