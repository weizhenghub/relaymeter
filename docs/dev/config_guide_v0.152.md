# 配置页「接入指南」改造（v0.152）开发文档

## 1. 用户的初始指令

> 配置页面的指导我觉得还是不够直观，写成这样会不会好一点？
> [附 README.docx，结构：转换模式 / 透传模式各自给「官方 Claude Code settings.json → 走中继 settings.json」对照 + 「为什么用 @@」解释]

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 配置页接入指南按 docx 结构重写 | 指令 |
| B | 转换模式块：加 Claude Code `settings.json` 官方 vs 走中继对照 | docx §转换模式 |
| C | 透传模式块：加 Claude Code `settings.json` 官方 vs 走中继对照 | docx §透传模式 |
| D | 透传模式块：解释「为什么用 @@ 格式」（占用 url 位置 + 重写 key 字段） | docx §透传模式 §「此数据包到达中继后…」 |
| E | 旧版 curl 自相矛盾（`/anything` + anthropic key + deepseek-chat model）需修正 | 设计自决（沿用 docx 假设：「举真实能跑的例子」） |

### 隐含但需要确认的点（设计自决）

- **表达形式**：docx 是纯文本 + 代码块，UI 渲染是 HTML+CSS —— **JSON 用左右两栏对照**（用户一眼看出「左边改哪几行变右边」），比 docx 的「上下两段」更直观。
- **改动 key 的视觉标记**：黄底高亮（不是绿底 / 红底）—— 黄是 warning 色，「这里要改」是中性提示，不是错误也不是成功。
- **流程图分段**：「客户端发 → 中继拆 → 上游收」三段 —— 再多一步（缓存 / 鉴权）打破简洁，再少一步（合并发和收）丢核心解释。
- **docx 里没强调的 curl**：保留但修正矛盾 + 进 `<details>` 折叠 —— 主区域只放「最常用配置」，curl 是高阶内容。
- **i18n**：所有新增文案走 `t()` 字典 —— 项目本身已是 5 语言架构（v0.151），新文案不能破坏。
- **CSS**：在现有 `.cfg-code` 系列后接续新增类，复用现有变量（`--border-soft` / `--surface-muted` / `--accent`），不引入新主题变量。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：定位现状

旧 `renderConfigPage` 透传模式块（app.js:1805 旧版）：

```
- cfg-mode-title
- cfg-guide-p (一段抽象描述)
- cfg-steps (2 步：开关 + 填 api-key 格式)
- cfgCode("透传 curl 示例", "curl ${base}/anything ... x-api-key: ...@@... sk-ant-xxx ... deepseek-chat")
```

问题：
1. 「透传 curl 示例」三处语义错位：路径 `/anything`（任意路径但配 anthropic x-api-key）+ `sk-ant-xxx`（Anthropic 风格 key）+ `model: deepseek-chat`（OpenAI 模型名）。
2. 抽象描述里「目标地址@@上游key」用户没概念 —— 没见过 docx 那种解释就完全 get 不到为什么。
3. 没有「官方 vs 中继」对照 —— 用户面对 Claude Code `settings.json` 时不知道改哪几行。

### 第二阶段：方案设计

**转换模式块**结构：

```
h2 转换模式 [默认·推荐 chip]
p 是什么
cfg-steps (2 步)
cfgJsonDiff  ← 新
  左：官方 Claude Code settings.json（指向 https://api.deepseek.com/anthropic）
  右：走中继（BASE_URL → 中继、AUTH_TOKEN → auto、MODEL → auto）
cfgCode Codex / OpenAI 客户端（env）  ← 保留兼容 OpenAI 客户端用户
```

**透传模式块**结构：

```
h2 完全透传模式
p 是什么
h3 为什么用 @@ 格式？
p 解释（占用 url 位置 + 重写 key 字段）
cfgWhyFlow  ← 新：客户端发 → 中继拆 → 上游收
cfgJsonDiff  ← 新
  左：官方 Claude Code settings.json
  右：走中继（AUTH_TOKEN 改为 "sk-xxx@@https://api.deepseek.com/anthropic"）
details.cfg-adv-curl  ← 新折叠
  summary 高级：curl 原生调用
  cfgCode curl /v1/chat/completions（OpenAI 入口 → DeepSeek）
  cfgCode curl /v1/messages（Anthropic 入口 → DeepSeek Anthropic 兼容）
```

### 第三阶段：helper 设计

**`cfgJsonDiff(labelOff, codeOff, labelRelay, codeRelay, markRelay)`**：

- 渲染 `.cfg-json-diff` grid 容器（`1fr 1fr`）
- 两边各一个 `.cfg-json-side`：`.cfg-json-side-head`（标签）+ `<pre><code>`（代码）
- 中继侧 head 加 `.cfg-mode-tag` 写「走中继」（用 accent 橙），与官方侧区分
- 中继侧代码逐行扫描，匹配 `^\s*"<key>":` 形式的行，若 key 在 markRelay 白名单则整行包 `<span class="cfg-chg">`（黄底 18% alpha）

**`cfgWhyFlow(step1, body1, step2, body2, step3, body3)`**：

- 渲染 `.cfg-why-flow` flex 容器
- 三段 `.cfg-why-step`：`.cfg-why-step-title`（小标题）+ body HTML（允许 code/small）
- 段间 `.cfg-why-arrow`（→）

### 第四阶段：实现路径

| # | 任务 | 依赖 |
|---|---|---|
| 1 | 新增 CSS 类（5 个新 selector） | 无 |
| 2 | app.js 新增 helper `cfgJsonDiff` + `cfgWhyFlow` | #1 |
| 3 | app.js `renderConfigPage` 转换模式块改写 | #2 |
| 4 | app.js `renderConfigPage` 透传模式块改写 | #2 |
| 5 | i18n 五语同步（26 条 key × 4 字典 = 104 条新增） | #3 #4 |
| 6 | cache stamp bump | #3 #4 |

### 第五阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 黄底 vs 绿底 vs 红底 | 黄底（warning 色） | 改动是中性提示，不是 error/success；黄底与 v0.149 / v0.150 警告色一致 |
| 三段流程图 vs 两段 / 四段 | 三段（发 / 拆 / 收） | 「拆」是中继的核心动作，少了它流程图就退化成「直连」；多了就冗余 |
| curl 进 `<details>` 折叠 | 是 | curl 是高阶用户的工具，普通用户不需要；放主区域会拉长滚动 |
| 双栏对比 vs 单栏「前 → 后」 | 双栏并排 | 用户能一眼看到「两边差异」；上下单栏需要视线上下扫，门槛更高 |
| 改动 key 黄底 vs 整段代码框染色 | 仅改动 key 行染色 | 用户聚焦的是「我要改哪里」，整段染色反而稀释焦点 |
| 不沿用 docx 的 export 命令 | 用 JSON | Claude Code 用户面对 settings.json，JSON 是其原生形态；export 命令要脑补 shell 上下文 |
| 旧版「透传 curl 示例」直接删 | 是 | 自相矛盾 + 用户可以从 docx 里找到等效内容；新版两个 curl 例子更准确 |
| 新版 curl 例子用 deepseek | 是 | docx 例子就是 deepseek；保持与 docx 一致便于用户对照 |

---

## 4. 实施细节

### 4.1 `cfgJsonDiff` 高亮算法

```js
function cfgJsonDiff(labelOfficial, codeOfficial, labelRelay, codeRelay, markRelay) {
  const linesOff = escape(codeOfficial).split("\n");
  const linesRel = escape(codeRelay).split("\n");
  const markSet = new Set(markRelay || []);
  const renderedRel = linesRel.map((line) => {
    const m = line.match(/^(\s*)"([^"]+)"\s*:/);
    if (m && markSet.has(m[2])) return `<span class="cfg-chg">${line}</span>`;
    return line;
  }).join("\n");
  // ... 两栏渲染
}
```

关键点：
- `escape()` 在 diff 前调用 —— 转义 JSON 里的 `<` / `>` / `"` 等
- `markSet` 用 `Set` —— 26 行 × 3 key 的扫描，O(1) 查表
- 正则 `^\s*"([^"]+)"\s*:` —— 匹配行首缩进 + 引号 key + 冒号，跳过嵌套 value 行

### 4.2 CSS 黄底配色

```css
.config-page .cfg-json-side pre code .cfg-chg {
  background: rgba(245, 158, 11, 0.18);  /* accent 橙 18% alpha */
  border-radius: 2px;
  padding: 0 2px;
}
```

不写死颜色（不直接 `#fff3cd`）：用 `--accent` 变量 + 18% alpha —— 主题切换（dark / day / light）时跟随 accent 变。

### 4.3 `cfgWhyFlow` 的 body 参数

body 是 HTML 字符串（不是纯文本）—— 允许 `<code>` / `<small>` 嵌入。例如：

```js
cfgWhyFlow(
  t("1. 客户端发"),
  `<code>ANTHROPIC_AUTH_TOKEN=<br>${base}/anthropic@@&lt;key&gt;</code><br><small>${t("base_url = 中继地址")}</small>`,
  ...
)
```

`<br>` 在 `<code>` 内强制换行 —— 默认 `<code>` 不换行；`<small>` 走小号字体的二级说明。

### 4.4 i18n 五语同步要点

26 条 key 全是新文案，4 字典 × 26 = 104 条翻译。难度分布：
- en：完全英文翻译（最长单条 ≈ 150 chars）
- zh-TW：S2T + 高频 UI 词 override（项目里已有 S2T 表）
- ja / ko：常用术语 override（如「设置」→「設定」/「설정」、「上游」→「アップストリーム」/「업스트림」），未覆盖 fallback en

新增 key 全部遵循「key 是简中原文 / value 是目标语言」模式 —— 与项目 i18n 约定一致，`t()` 函数才能正确回退到原文。

---

## 5. 验收清单

### 5.1 视觉

- [ ] 转换模式块：左右两栏 JSON 对照，左「官方」右「走中继」；右栏 3 行 key 黄底（ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_MODEL）
- [ ] 透传模式块：「为什么用 @@ 格式？」h3 + 解释段 + 三段流程图（发 / 拆 / 收）+ 第二个 JSON 对照（只高亮 ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN）
- [ ] 透传模式块：「高级：curl 原生调用」可折叠，点击展开两个 curl 代码块
- [ ] 切换主题（dark / day / light）黄底颜色跟随 accent 变量
- [ ] 切换语言（5 种）：所有 t() 翻译正确

### 5.2 功能

- [ ] 旧版「透传 curl 示例」消失（被两个新 curl 替代，且移入折叠块）
- [ ] 复制按钮仍工作（`wireConfigPage` 的 click handler 不变，新容器 `.cfg-code` / `.cfg-json-side pre code` 都在 copy 选择器内）
- [ ] 配置页 sig 短路仍工作（cfg 块内容变化不会触发 sig 重算 —— 模板字符串变化不计 sig）

### 5.3 边界

- [ ] i18n 缺翻译的语言（理论上不应有）：`t()` 回退原文，UI 显示中文（acceptable）
- [ ] 端口变化（lastSnap.port 改）：`base` 变量重新拼接，JSON 对照里 `${base}/anthropic` 自动更新
- [ ] 透传模式开关切换：不影响配置页（指南是只读，不显示 active 状态）

---

## 6. 数据流

```
启动 → 渲染配置页：
  lastSnap / lastStatus → renderConfigPage()
    ├─ base = "http://127.0.0.1:" + port
    ├─ cfgJsonDiff(off_json, relay_json, markRelay)
    │   └─ markRelay 数组里列出的 key → 中继侧黄底
    ├─ cfgWhyFlow(step1, step2, step3)  ← 仅透传块
    └─ <details> + cfgCode curl ×2     ← 仅透传块
用户切换语言 → applyLang()
  └─ 遍历 .cfg-guide-p / h2 / h3 / .cfg-json-side-head / .cfg-why-step-title / .cfg-mode-tag / summary
     → _curDict()[zh原文] 替换 → 各语言翻译
```

---

## 7. 沉淀 / 风险

### 设计沉淀

- **「JSON 对照 + 流程图」是 UI 解释类文本的强模式**：用户面对自己熟知的形态（settings.json）+ 三步流程（发 / 拆 / 收），认知负担最低。下次解释类似「中继做了什么」时优先用此模式。
- **黄底（warning 色）标记改动**比绿底 / 红底更中性 —— 适用于「这里要改」的所有 UI 场景，可复用。
- **`<details>` 折叠是「高阶内容下沉」的好容器**：不影响主流程阅读，又能让需要的用户找到。CSS 已写好（`.cfg-adv-curl` 系列），下次类似场景复用即可。

### 风险

- **JSON 对照硬编码了 deepseek 例子**：未来若用户用其他上游（OpenRouter / 硅基流动），对照例子与实际不符 —— 解决办法是 v0.153 加「按 upstream 自动选择例子」（按 lastSnap.active_per_platform 切换），但本次不动。
- **流程图 body 字符串里的 HTML**：用户输入的 base 变量若含 `<` / `>` 会被浏览器解析 —— 用了 `${base}/anthropic` 拼接，base 是 `http://127.0.0.1:port` 形式不会含特殊字符；但若未来 base 来源变化（用户自定义端口格式）需要重新 escape。当前安全。
- **i18n 翻译质量**：en / ja / ko / zh-TW 翻译是新写的，没有 native 校对 —— 长期可能发现用词不当；按 v0.151 节奏接受「先用着 + 用户报问题后改」。
- **CSS `cfg-chg` 用 rgba(245,158,11,0.18)**：在 dark 主题下黄底与代码块底色对比度偏低，可能视觉上不够明显 —— 当前未做主题分支；用户反馈后加 `--cfg-chg-bg` 变量。

### 不在本次范围

- 配置页其他部分（active 上游 / 快捷切换 / 已配置上游）—— 仍在 `<details class="cfg-adv">` 折叠，未动
- 上游配置页（设置 → 上游）—— 独立的卡片，与配置页不同
- 按 active 上游自动切换 JSON 例子 —— v0.153 任务
- CSS 主题分支的 `cfg-chg` 颜色 —— 留接入点，v0.153 处理