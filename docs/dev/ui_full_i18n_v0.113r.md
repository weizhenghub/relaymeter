# v0.113r 全站 i18n 收尾

## 1. 原始指令

> 切换语言时，并没有所有地方都切换，很多按钮和内部容器都是保留的中文。请再度扫描整个程序，分析所有地方

v0.113n 已把语言切换装好（设置页全量覆盖），但用户在 EN 模式下仍看到大量中文残留（统计卡表头、历史行表头、弹窗 select 选项、设置子菜单项、flash 渐变编辑器、cfg-adv 折叠指南……）。

## 2. 提炼实现点

1. **统一字典**：以「中文原文为 key」扩展到 ~200 条；空态、表头、按钮、弹窗选项全收。
2. **applyLang 重写为全文档叶子翻译 + 元素级 data-i18n 双轨**：
   - **元素级**：`[data-i18n]` 显式映射解决同名 key 撞车（按钮「启动」 vs 分组「启动」）。
   - **叶子级**：可见 .view + body > chrome 的 TreeWalker，命中字典就翻，天然覆盖嵌套与未知选择器。
3. **可见性判定**：hidden 属性 + computed style 双判，跳过 hidden overlay 与 display:none 子树（防止把 hidden 内容也"翻译"了浪费）。
4. **zh 还原缓存**：每个 textNode 首见 `__i18nZh = nodeValue`；脚本 render 写入新中文 → 重新捕获；切回 zh 时按缓存还原。
5. **acceptNode 双模式**：zh→en 挑「含中文」节点；en→zh 挑「有缓存且当前是英文」节点（否则翻后英文节点永远不被访问）。
6. **异步渲染兜底**：`MutationObserver` 监听 body，rAF/setTimeout 去抖重跑 applyLang —— 解决 renderStats 每 tick async 重写空态把 tick 末尾 applyLang 翻译抹掉的问题。
7. **侧栏子菜单 + flash 高级编辑器**：display:none 默认隐藏 → walker 跳过 → 直接给元素加 `data-i18n` 元素级映射（不受 offscreen 影响）。
8. **独立窗口 live_panel**：自建 `LP_I18N` + `window.__lpT` + storage event 同步（与主栏独立）。
9. **stats hint 拼接 t() 化**：`renderStats` 里 `${range} · 按${dim}` 改为 `I18N.en[rangeZh]` 显式查表，避免字典"近 30 天 · 按上游"这种整串匹配依赖。

## 3. 需求分析 / 开发路径

### 需求

用户在 EN 模式下，程序任何可见区域都不应残留中文。

### 关键设计

- **零侵入**：不动 render 模板；只在字典补键 + applyLang 重写。
- **数据源**：每个 textNode 的首见 nodeValue（render 写的中文就是原文）。
- **缓存策略**：render 函数会反复写同一句（"暂无数据"），用 `txt === __i18nZh` 判定「同值不覆盖」避免误重置；render 写新中文时重置缓存。
- **可见性**：display:none 祖先的子节点 walker 跳过 —— 用户看不到，不必翻；display:fixed 的 modal overlay 用 hidden 属性判定。

### 难点（已解决）

- **数据节点 vs 元素节点冲突**：`__i18nZh` 同时挂在 element（element-level）与 textNode（leaf-level）上，互不干扰（Element 接口不冲突 Text 接口）。但元素级分支的"内容变了就重新捕获"逻辑被英文翻译误触发 —— cur === enVal 时跳过覆盖 zh0。  
- **zh 还原跑空**：en→zh 时 acceptNode 仍用「含中文」过滤，结果翻后的英文节点不被访问 → 加分支：有 `__i18nZh` 缓存且当前不是缓存值就接受。  
- **hidden sidebar 子菜单**：body.is-settings 切换后 nav-sub-item 从 display:none 变 flex，walker 第一次跑已捕获 → setView 末尾强制 applyLang 兜底。  
- **flash 高级编辑器**：默认无 .show class → display:none，walker 跳过 → 8 个文本节点直接 data-i18n 元素级。  

## 4. 问题 → 解决

| 问题 | 解决 |
|---|---|
| renderStats 每 tick async 重写空态，tick 末尾 applyLang 追不上 | MutationObserver + setTimeout(0) 去抖（rAF 在 headless 不触发） |
| stats hint `${range} · 按${dim}` 字典需要"近 30 天 · 按上游"整串键 | 改为逐段查表 + t() |
| applyLang 元素级分支把 EN 译文当作新 zh 覆盖 | 加 cur !== enVal 守卫 + 中文检测 |
| applyLang 叶子级 zh 还原 acceptNode 过滤英文节点 | 双模式 acceptNode：有缓存 + 当前非缓存值 |
| body.is-settings 切换后子菜单 walker 跳过 | setView 末尾 applyLang() |
| flash 高级编辑器 default display:none | 8 节点加 data-i18n 元素级 |
| 字典撞车（按钮「启动」 vs 分组「启动」） | data-i18n="settings-group.startup" 显式 key |
| live_panel 独立窗口（独立 localStorage / DOM） | LP_I18N + storage event 同步 |

## 5. 是否完全按规划

✅ 完全按规划。无新增传输层、无后端协议改动、无 relay 进程触达；改动只在 `src/relay/web/` 4 个文件 + 字典扩张。

## 6. 最终实现点

### 改动文件

| 文件 | 改动 |
|---|---|
| `src/relay/web/app.js` | applyLang 重写（元素级 + 叶子级双轨）；MutationObserver 兜底；setView 末尾 applyLang；stats hint t() 化；字典 ~250 条 |
| `src/relay/web/index.html` | nav-sub-item 6 项 data-i18n；版本 bump `app.js?v=20260822-25` |
| `src/relay/web/live_panel.js` | LP_I18N + applyLivePanelLang + 状态缓存 |
| `src/relay/web/live_panel_grid.js` | 用 `window.__lpT` 替代硬编码 label |

### 关键代码

`app.js` applyLang（节选）：
```js
function applyLang() {
  document.documentElement.lang = I18N.lang === "en" ? "en" : "zh-CN";
  document.body.classList.toggle("lang-en", I18N.lang === "en");
  setSegLang(I18N.lang);
  // 元素级：data-i18n 显式映射（撞车消歧）
  document.querySelectorAll("[data-i18n]").forEach((el) => { ... });
  // 叶子级：可见 .view + body > chrome 全文档 walker
  const roots = [];
  document.querySelectorAll(".view").forEach((v) => { if (!v.hidden) roots.push(v); });
  document.querySelectorAll("body > *").forEach((n) => {
    if (n.id === "__report") return;
    if (n.tagName === "SCRIPT" || n.tagName === "STYLE") return;
    if (!n.matches(".view")) roots.push(n);
  });
  roots.forEach((root) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        // ... visibility / 中文 检测 + 双模式 acceptNode
        if (I18N.lang === "en") {
          if (!txt || !/[\u4e00-\u9fff]/.test(txt)) return NodeFilter.FILTER_REJECT;
        } else {
          if (n.__i18nZh == null) return NodeFilter.FILTER_REJECT;
          if (txt === n.__i18nZh) return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    let node;
    while ((node = walker.nextNode())) {
      // 缓存策略 + 翻译 / 还原
    }
  });
}
```

## 7. 验证

1. **地毯扫描 EN 模式（隐藏 modal 排除）**：0 中文残留（侧栏子菜单、flash 编辑器、config cfg-adv 折叠、stats 空态、live_panel 均已翻译）。
2. **toggle 往返（zh → en → en-overview → zh-back）**：nav 总览/设置 往返正确；group startup (data-i18n 消歧) 与 storage name (data-i18n) 往返正确；token 卡 EN 翻译仅在 overview view 可见时翻译（hide 时不浪费，view 切换回 overview 时翻译）。
3. **live_panel EN**：`#lp-badge/.live-panel-label/.live-panel-summary/.live-panel-token-label/.live-panel-cache-rate/#lp-key/#lp-status/#lp-thinking/#grid-empty/.live-panel-key/title/.win-ctrl-close` —— CLEAN。
4. **字典覆盖**：~250 条键（中继模式 / 透传模式 / 上游状态 / 启动 / 重启 / 状态文本 / 各种空态 / 表头 / 按钮 / 弹窗选项 / 配置指南长句 / 设置页所有分组与项）。
5. **异步兜底**：stats 视图在 1s/3s 探针点均为空态翻译（MutationObserver 在 headless 虚拟时间下用 setTimeout 触发，避开 rAF 不响应）。
6. **MutationObserver 不会自激**：applyLang 幂等 —— 已翻译节点再次 walker 时 nodeValue === cached → 不写。
7. **回归**：`node --check` 三个 JS 文件全 OK；用户视角下所有 EN 模式可见区域无中文。