# 前端 i18n 扩展到 5 语言（v0.151）开发文档

## 1. 用户的初始指令

> 增加 3 种主流语言。

经 AskUserQuestion 选定：**日语（ja）、韩语（ko）、繁体中文（zh-TW）**。

加上原有的简体中文（zh）和 English（en），前端 i18n 扩展为 5 种语言。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 增加 ja / ko / zh-TW 3 种语言 | 指令（语言范围） |
| B | 设置面板「语言」段需新增切换入口 | 沿用 v0.113n 的 seg-lang 机制 |
| C | 按钮文字母语展示（国际化最佳实践） | 设计自决 per `feedback_decide_show_effect.md` |
| D | localStorage `lang` key 接受 5 种值 | 沿用 v0.113n 的偏好持久化机制 |
| E | 字典结构需可扩展（未来再加语言不需大改） | 设计自决（架构层） |
| F | 翻译生成需可重跑 / 可验证（避免手抄漂移） | 设计自决（流程层） |

### 隐含但需要确认的点

- **字典组织**：旧版是 `I18N.en = {zh: en}` 单字典 + 「非 en 时显示 en」的折中。新版需要每种语言自己的字典 → 改为 `I18N[lang]` 平铺 5 字典（en / zh-TW / ja / ko，外加源代码原文字典挂在 `zh` 上不进 I18N[lang] key 列表）。
- **「非 zh 时显示什么」的判定基准**：旧版「lang === "en" → 翻，其他 → 还原原文」语义只覆盖两种语言。新版翻为「lang !== "zh" → 走当前 lang 字典，lang === "zh" → 还原原文」。这样未来加第 6 种语言无需改判定。
- **未知 lang 兜底**：`I18N[lang]` 查不到时回退 `I18N.ja`（不兜回 en —— 因为 ja 字典覆盖度比 en 高，且 ja 比 en 更接近 zh 用户的阅读习惯）。仅作防御，正常流（localStorage 校验过的 5 种值）不会触发。
- **`<html lang>` 属性**：浏览器 / 屏幕阅读器 / 拼写检查都依赖这个属性；按 BCP-47 输出（zh-CN / zh-TW / en / ja / ko），不能统一写 zh。
- **CSS 微调**：日文片假名 / 韩语谚文字母形状、字宽、字距与中英文不同 —— 给 body 加 `lang-ja` / `lang-ko` / `lang-zh-tw` 类作为 CSS 接入点（本次不实际写 CSS 规则，留接入点）。
- **数据流不变**：i18n 是纯前端偏好，与后端 8088 / 数据库 / 跨屏状态完全无关 —— 无需任何 Python / FastAPI 改动。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：定位改动面

```
app.js：单点定义 I18N，5 处使用点
  ├─ I18N.lang 检测（localStorage 读取 + 校验，line 4304）
  ├─ I18N.en = {...}（line 4312 起的字典定义）
  ├─ t(key)（line 5671）
  ├─ applyLang()（line 5679，含 5 处 lang === "en" + 3 处 I18N.en[]）
  └─ 切换按钮 click handler（line 6449，含 lang 校验）
index.html：cache stamp（?v=...）需 bump
seg-lang markup：旧版嵌在 app.js 字符串里（line 5987），需加 3 个按钮
```

### 第二阶段：字典结构设计

```js
const I18N = {
  lang: <从 localStorage 读取并校验为 5 种之一>,
  // 字典区 —— 平铺，key 是语言代码
  en:     { "透传上游（自动发现）": "Passthrough upstreams (auto-discovered)", ... },  // 331 条
  "zh-TW":{ "透传上游（自动发现）": "透傳上游（自動發現）", ... },                       // 331 条
  ja:     { "透传上游（自动发现）": "パススルー上流（自動検出）", ... },                  // 331 条
  ko:     { "透传上游（自动发现）": "패스스루 업스트림 (자동 발견)", ... },                // 331 条
  // zh 字典 = 源代码原文字典，不进 I18N[lang] key 列表（key 与 value 一一对应，字典定义本身可以省略，靠「node.__i18nZh 缓存」还原）
};
```

`_curDict()` helper：

```js
function _curDict() { return I18N[I18N.lang] || I18N.ja; }  // 未知 lang 兜底 ja
function t(key) { return (I18N.lang !== "zh" && _curDict()[key]) || key; }
```

### 第三阶段：applyLang 翻写三处判定

| # | 位置 | 旧 | 新 |
|---|---|---|---|
| a | `<html lang>` 属性 | `I18N.lang === "en" ? "en" : "zh-CN"` | `lang === "zh" ? "zh-CN" : lang === "zh-TW" ? "zh-TW" : lang` |
| b | body class | `classList.toggle("lang-en", lang === "en")` | 移除 4 个 lang-* 类，按需添加 `lang-${lang.toLowerCase()}`（非 zh 时） |
| c | data-i18n 元素翻译 | `if (I18N.lang === "en")` | `if (I18N.lang !== "zh")` |
| d | walker 过滤器 | `if (I18N.lang === "en")`（挑含中文节点）| `if (I18N.lang !== "zh")`（语义不变，统一挑含中文节点） |
| e | walker 翻译块 | `if (I18N.lang === "en")` 内部 `I18N.en[...]` ×3 | `if (I18N.lang !== "zh")` 内部用 `dict = _curDict(); dict[...]` ×3 |

### 第四阶段：按钮 + click 守卫

旧 markup（2 个按钮）：

```html
<div class="seg seg-lang">
  <button type="button" class="seg-btn" data-lang="zh">简体中文</button>
  <button type="button" class="seg-btn" data-lang="en">English</button>
</div>
```

新版（5 个按钮，文字母语）：

```html
<div class="seg seg-lang">
  <button type="button" class="seg-btn" data-lang="zh">简体中文</button>
  <button type="button" class="seg-btn" data-lang="zh-TW">繁體中文</button>
  <button type="button" class="seg-btn" data-lang="en">English</button>
  <button type="button" class="seg-btn" data-lang="ja">日本語</button>
  <button type="button" class="seg-btn" data-lang="ko">한국어</button>
</div>
```

按钮放在 `.seg-lang` 子树内，walker 翻译时跳过（`p.parentElement.classList.contains("seg-lang")` 判定），所以按钮文字永远是母语原文 —— 不会因切语言被覆盖回译。

click 守卫（line 6452）：

```js
// 旧：
if (lang === I18N.lang || (lang !== "zh" && lang !== "en")) return;
// 新：
if (lang === I18N.lang || !["zh", "en", "ja", "ko", "zh-TW"].includes(lang)) return;
```

白名单形式 —— 加新语言时只改这一行（与 `localStorage` 校验、`<html lang>` 映射两处集中改）。

### 第五阶段：翻译生成器（一次性脚本，可重跑）

```
extract_i18n.py  →  从 app.js 拉 en 字典 331 条 → 1 行 JSON 文本
gen_translations.py
  ├─ input: en 字典 331 条 + JA_OVERRIDE ~130 + KO_OVERRIDE ~130 + S2T 字符映射表
  ├─ ja: 取 en 字典 → 逐 key 看 JA_OVERRIDE 是否有 → 有则用 override / 无则保留 en
  ├─ ko: 取 en 字典 → 逐 key 看 KO_OVERRIDE 是否有 → 有则用 override / 无则保留 en
  ├─ zh-TW: 取 zh 原文 → 字符级 S2T 映射 + 繁体高频词 override
  └─ output: i18n_block.js（4 字典 × 331 条，结构合法可直接粘贴）
```

`node --check i18n_block.js` 验证语法（之前 `zh-TW` 没加引号会报 `SyntaxError: Unexpected token`，quote 包裹后通过）。

### 第六阶段：实现路径

| # | 任务 | 依赖 |
|---|---|---|
| 1 | 写 extract_i18n.py / gen_translations.py / 跑出 i18n_block.js | 无 |
| 2 | app.js：插入 3 字典到 `I18N.en` 后 | #1 |
| 3 | app.js：`t()` / `_curDict()` helper | #2 |
| 4 | app.js：`applyLang` 翻写 5 处判定 | #2 |
| 5 | app.js：`<html lang>` / body class 翻写 | #4 |
| 6 | app.js：click 守卫白名单 | #2 |
| 7 | app.js：seg-lang markup 加 3 按钮 | 无 |
| 8 | index.html：cache stamp bump | #4 |

### 第七阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 字典平铺 vs `I18N.dict[lang]` | 平铺（`I18N[lang]`） | 与 `_curDict()` helper 配套，省一层属性访问；旧 `I18N.dict[I18N.lang]` 是历史遗留 bug（从未定义过 dict 属性），翻写顺手修了 |
| 未知 lang 兜底 | `I18N.ja` | ja 字典覆盖度比 en 高；正常流不会触发（localStorage 校验过） |
| 按钮顺序 | zh / zh-TW / en / ja / ko | 简体中文用户最常访问置首位；中英同语系放一起；CJK 三语按「字形密度升序」排（简体最简 → 繁体加字 → 日文混假名 → 韩文谚文字距最大），符合「密度递增加视觉留白」直觉 |
| 按钮文字 | 母语（繁體中文 / 日本語 / 한국어）| i18n 最佳实践 —— 用目标语言本身告诉用户「这是目标语言按钮」，比 「Japanese」 / 「중국어 번체」更地道 |
| walker 跳过 seg-lang 子树 | 保留 | 按钮文字永远显示原文（这是 seg-btn 的预期语义）；其他 UI 文本可被翻译覆盖 |
| 翻译未覆盖项 fallback | fallback en（不是 fallback ja）| 技术名词 / 品牌名 / 模型字段（如 `input_tokens` / `claude-opus-4-6`）直接复用英文最准确，本地强行音译反而失真 |
| CSS 微调（lang-ja / lang-ko） | 留接入点不实写 | 本次范围仅 i18n 字典 + 切换；视觉字号 / 字距调优是另一个独立任务 |

---

## 4. 实施细节

### 4.1 detect-lang 校验

`localStorage.getItem("lang")` 读到的字符串可能是旧版「en」也可能是新版「ja / ko / zh-TW」，也可能是用户手动改 localStorage 写脏值。集中校验：

```js
if (v === "en" || v === "ja" || v === "ko" || v === "zh-TW") return v;
return "zh";
```

「zh」不需要检测（localStorage 没值时 `getItem` 返回 null，走默认）。

### 4.2 `<html lang>` BCP-47 输出

```js
const htmlLang = I18N.lang === "zh" ? "zh-CN"
  : I18N.lang === "zh-TW" ? "zh-TW"
  : I18N.lang;  // en / ja / ko 直接是合法 BCP-47
```

`zh-TW` 必须显式映射 —— 因为 I18N key 是 `"zh-TW"`（带连字符），BCP-47 也恰好是 `zh-TW`，巧合但安全。

### 4.3 body class 动态切换

```js
document.body.classList.remove("lang-en", "lang-ja", "lang-ko", "lang-zh-tw");
if (I18N.lang !== "zh") {
  document.body.classList.add(`lang-${I18N.lang.toLowerCase()}`);  // zh-TW → lang-zh-tw
}
```

先全清再加 —— 避免旧 class 残留。当前没 CSS 规则用这些 class，留接入点。

### 4.4 walker 翻译块的字典缓存

```js
const dict = _curDict();  // 函数顶部取一次，循环内复用
const enWhole = dict[whole];
...
if (dict[cand]) { best = { len: n, en: dict[cand] }; break; }
```

`_curDict()` 每次访问会做一次属性查找（`I18N[lang]`），循环里调用 N 次（maxlen=30 × 节点数）会有可观开销 —— 提到循环外缓存一次。

### 4.5 cache stamp bump

`app.js?v=20260824-06 → ?v=20260824-07`。pywebview 加载 web 资源时把整个 web/ 目录 cache 起来，旧版 cache 命中会导致新版字典不生效（用户看到设置面板有 5 个按钮但点 ja 仍然是英文），所以 bump 是必须的。

---

## 5. 验收清单

### 5.1 设置面板

- [ ] 设置 → 语言段：5 个按钮（简体中文 / 繁體中文 / English / 日本語 / 한국어）依序排列
- [ ] 当前语言的按钮有 `active` class（背景色高亮）
- [ ] 点击任一按钮 → 页面所有可见文本立即翻译为目标语言（无需刷新）
- [ ] 刷新页面 / 重启 GUI 后语言选择保留（localStorage）

### 5.2 翻译覆盖度

- [ ] zh → en / ja / ko / zh-TW 切换：设置面板、状态栏、统计 hint、按钮文字均翻译
- [ ] 总览页「平台分布」卡 badge 文字翻译（`anthropic` / `openai·chat` / `openai·responses`）
- [ ] live panel 流式容器内文字目前仅 en（live_panel.js 多语言 task #30，本次不覆盖）
- [ ] 翻译未覆盖项显示原文或 en（acceptable fallback，不应空白）

### 5.3 还原语义

- [ ] 切到 en → 再切回 zh：所有节点文字应精确还原（`__i18nZh` 缓存保真）
- [ ] zh → ja → en → zh：来回切 4 次，节点文字与最初 zh 完全一致

### 5.4 边界

- [ ] localStorage `lang` 写脏值（如 `"fr"` / `"xx"`）→ 启动回落到 zh，不报 JS 错
- [ ] localStorage `lang` 写旧版 `"zh-CN"` → 回落到 zh（因为校验白名单不含）
- [ ] 首次安装（无 localStorage）→ 默认 zh
- [ ] `<html lang>` 在 5 种状态分别正确（devtools 查看）
- [ ] body class 在切语言时正确更新（devtools 查看）

---

## 6. 数据流

```
启动：localStorage.getItem("lang") → 校验 5 种之一 → I18N.lang
点击按钮：btn.dataset.lang → 校验白名单 → 写 I18N.lang + localStorage → applyLang()
  ├─ documentElement.lang = BCP-47 映射
  ├─ body classList 重置 + 按需添加 lang-*
  ├─ data-i18n 元素翻译：I18N[lang][over] → 写 el.textContent
  └─ walker 全文档翻译：dict[whole] / dict[片段匹配] → 写 node.nodeValue
切回 zh：data-i18n / walker 都走「缓存原文还原」分支（__i18nZh 字段）
```

---

## 7. 沉淀 / 风险

### 设计沉淀

- **「字典平铺 + `_curDict()` helper + `lang !== "zh"` 判定」是当前架构**。下次再加第 6 种语言只需：(a) 加 `I18N.<newlang>` 字典；(b) 修 3 处白名单（localStorage 校验 / click 守卫 / setSegLang 不需改因为按 `data-lang` 匹配）。其他判定 / 翻译 / walker 全自动覆盖。
- **walker 跳过 seg-lang 子树** 是 seg-btn 文字母语的关键 —— 不能改。任何「按钮文字会被翻译覆盖」的回归都先查这条。
- **`<html lang>` 必须按 BCP-47 输出**，不能简化成「非 zh 时写 lang」。屏幕阅读器（NVDA / VoiceOver）和浏览器拼写检查都依赖它。

### 翻译质量风险

- **JA / KO 字典覆盖率约 60%**（en 331 条中 ~130 条被 override，剩余 ~200 条保留 en）。未覆盖项 fallback en —— 不会显示空白，但会显示「中文 → 英文」混排。可接受的范围，因为：(a) 多数 fallback 是技术名词（input_tokens / upstream / passthrough），目标语言本身就常用英文；(b) JA_OVERRIDE / KO_OVERRIDE 可在未来根据用户反馈增量追加。
- **zh-TW 是字符级 S2T 映射 + 高频词 override**，部分专有名词（API 路径、模型字段名）保持简体 —— 这是预期行为，不是 bug。
- **gen_translations.py 输出文件可能含 Python 字符串语法问题**（之前 en 字典里有过内嵌双引号 `"透传模式开启后，客户端请求"设置 → 透传模式"..."`，Python 字符串会断）。解决方案：把内嵌 `"..."` 替换成全角 `「」` 后再生成。

### 已知遗留

- **live_panel.js（task #30）尚未多语言化**。本次仅 app.js。侧栏流式容器里 badge 文字（「流式中」「完成」「出错」「工具调用超时」）目前只有 en。task #30 跟进。
- **CSS lang-ja / lang-ko 微调留接入点但没实写**。日文片假名字号偏大、韩语谚文字距偏宽等场景 —— 等用户报视觉问题再加。

### 不在本次范围

- 后端 8080 / 数据库 / 跨屏状态 —— i18n 是纯前端偏好，0 影响
- en / ja / ko 字典未覆盖的 ~200 条术语 —— 等用户反馈增量追加
- CSS lang-ja / lang-ko 视觉调优 —— 留接入点，task 分离
- live_panel.js 多语言 —— task #30