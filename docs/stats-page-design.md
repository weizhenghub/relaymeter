# v0.100.1 统计页（高级可视化）设计

## 1. 目标

侧栏菜单「统计」页面 (`data-view="stats"`) —— 8 张 D3.js + 原生 SVG 高级图，覆盖
**分布 / 趋势 / 流向 / 时间网格 / 配额** 五大维度，给"想要看更深的数据"的
用户一个独立的数据研判面板。

与「总览」的关系：总览侧重**当前状态快照**（spotlight + 6 张卡 + 实时波纹），
统计侧重**多维聚合与可视化**（冷数据、探索）。两者共用 SQLite requests 表，**不
增加 schema 字段**（详见第 6 节"取舍说明"）。

## 2. 页面布局

```
┌────────┬─────────────────────────────────────────────────────────────────┐
│        │  [ 时间段: 近 24h | 近 7 天 | 近 30 天 ]  [刷新]   last update: ...   │
│        ├─────────────────────────────────────────────────────────────────┤
│        │ ┌──────────────────────────────┐ ┌─────────────────────────────┐│
│ sidebar│ │ ① 平台分布 (donut)             │ │ ② Token 占比 (rose)            ││
│        │ │  anthropic / openai           │ │  input / output / cr / cc   ││
│ (其它) │ └──────────────────────────────┘ └─────────────────────────────┘│
│        │ ┌─────────────────────────────────────────────────────────────────┐│
│        │ │ ③ 请求 & 错误趋势 (stacked area, 按上游拆色)                  ││
│        │ │                                                                ││
│        │ └─────────────────────────────────────────────────────────────────┘│
│        │ ┌──────────────────────────────┐ ┌─────────────────────────────┐│
│        │ │ ④ 路由热力 (upstream × platform)│ │ ⑤ 上游 → 平台 Sankey        ││
│        │ └──────────────────────────────┘ └─────────────────────────────┘│
│        │ ┌─────────────────────────────────────────────────────────────────┐│
│        │ │ ⑥ 日历热力 (全年按日 token)                                     ││
│        │ └─────────────────────────────────────────────────────────────────┘│
│        │ ┌──────────────────────────────┐ ┌─────────────────────────────┐│
│        │ │ ⑦ 配额仪表盘 (D3 gauge, 按上游)│ │ ⑧ 每日/小时 stacked area     ││
│        │ └──────────────────────────────┘ └─────────────────────────────┘│
└────────┴─────────────────────────────────────────────────────────────────┘
```

**网格**：8 张卡 = 2 行 4 列 + 4 行单卡混合。`grid-template-columns: repeat(4, 1fr)`，
单卡占 `grid-column: span 2`，双卡占 `span 2`，全宽占 `span 4`。
**响应式**：窄屏退化为 `repeat(2, 1fr)`，每张卡 `span 2`（双卡并列）。

## 3. 8 张图规格

### ① 平台分布 donut
- **数据**：`{anthropic: total_tokens, openai: total_tokens}`（按平台 sum 4 token）
- **可视化**：donut（外环）+ 中心标签（"总 X tok"）
- **交互**：悬停高亮扇区 + tooltip（"anthropic · 1.2M tok · 65% · 142 req"）
- **API**：`GET /api/stats/aggregate?range=7d&dim=platform&top=2`
  - 复用 v0.99 的 `tui.fetch_aggregate_by_dim`，无新增后端代码
- **D3 模块**：`d3.pie() + d3.arc() + d3.scaleOrdinal(d3.schemeTableau10)`
  - 柱状+线条的扩展，路径 `arc.padAngle(0.02).cornerRadius(4)`

### ② Token 占比 nightingale rose
- **数据**：`{input_tokens, output_tokens, cache_read, cache_creation}`
- **可视化**：南丁格尔玫瑰图（半径 ∝ token 数，圆心角均分 90°×4）
- **交互**：悬停高亮瓣 + tooltip（"输入 · 4.5M tok · 38% · 12 req"）；切换"按请求数 / 按 token"
- **API**：`fetch_aggregate_by_dim` 按 platform 拆分后聚合 4 个 token 列；新增
  `tui.fetch_token_breakdown(*, since, dim)` 复用 requests 表 4 个 sum
- **D3 模块**：`d3.pie({startAngle: -Math.PI/2}) + d3.arc().innerRadius(40).outerRadius(d=>d.value/max*r)`

### ③ 请求 & 错误趋势（stacked area，按上游拆色）
- **数据**：`timeseries[upstream] = [{ts, requests, errors, total_tokens}, ...]`，按小时桶
  或日桶（取决于 range）
- **可视化**：双层 stacked area —— 上层 `requests`（各上游堆叠），下层 `errors`
  （每上游的错误线或半透明条）
- **交互**：图例点击隐藏/显示对应上游；x 轴拖拽缩放；tooltip 跨上游
- **API**：`GET /api/stats/timeseries?range=7d&bucket=hour|day&metric=requests|errors|total_tokens`
  - 新增 `tui.fetch_timeseries(db, *, since, bucket, metric, dim)` —— GROUP BY 桶 + dim
- **D3 模块**：`d3.stack() + d3.area().curve(d3.curveMonotoneX) + d3.scaleTime()`
  - brush 缩放用 `d3.brushX()`

### ④ 路由热力（upstream × platform 网格）
- **数据**：`{upstream: {anthropic: n, openai: n}, ...}`（请求数矩阵）
- **可视化**：方格热力（颜色深浅 = 请求数 log scale；每格写数字）
- **交互**：悬停高亮行/列 + tooltip
- **API**：`GET /api/stats/route_heatmap?range=7d` —— 新增
  `tui.fetch_route_heatmap(*, since)` 输出 `{upstream, anthropic_n, openai_n}`
- **D3 模块**：`d3.scaleSequential(d3.interpolateInferno) + d3.scaleBand() + d3.axisBottom/Left`

### ⑤ 上游 → 平台 Sankey
- **数据**：`{nodes: [{name: "上游A"}, {name: "anthropic"}], links: [{source: 0, target: 1, value: 142}, ...]}`
- **可视化**：双向 Sankey（左侧上游节点 → 右侧平台节点，链接粗细 = 请求数）
- **交互**：拖拽节点；点击高亮上下游链路；tooltip 显示流量数值
- **API**：`/api/stats/route_heatmap` 已提供，按 source=upstream / target=platform /
  value=requests 即可构造 Sankey
- **D3 模块**：`d3.sankey() + d3.sankeyLinkHorizontal()`（sankey 是 d3-sankey 子模块，
  单独引入 `d3-sankey.min.js` 或者直接用完整 d3 v7 内置 sankey 路径）

### ⑥ 日历热力（GitHub-style 全年日历）
- **数据**：`{date: 'YYYY-MM-DD', total_tokens, requests}` 最近 365 天
- **可视化**：7 行 × 53 列日历网格，每格颜色 = 0 到 max 渐变；空日期灰色
- **交互**：悬停 tooltip（"2026-08-19 · 1.2M tok · 142 req"）；左右箭头翻年
- **API**：`GET /api/stats/daily?days=365`（v0.99 已有，扩展 days 上限）
- **D3 模块**：`d3.scaleTime() + d3.scaleSequential(d3.interpolateOrRd)` +
  `d3.timeFormat("%Y-%m-%d")`

### ⑦ 配额利用率仪表盘
- **数据**：复用 v0.99 `fetch_by_upstream_with_costs` 的 `used_5h / quota_5h / utilization_5h`
- **可视化**：每个上游一行 D3 gauge（半圆环形进度条 + 中心 % 文字 + 颜色按 warning_level
  切换 ok/warn/critical/exhausted）
- **交互**：hover 显示 ETA + 5h_release
- **API**：`/api/upstreams` 已含，**无新后端**
- **D3 模块**：`d3.arc().innerRadius(50).outerRadius(70) + d3.scaleLinear([0, quota], [-π/2, π/2])`

### ⑧ 每日/小时 stacked area
- **数据**：按时间桶（小时/日）的 input/output/cache_read/cache_creation 4 条 stacked area
- **可视化**：4 层堆叠面积，颜色对应 token 类型（与 spotlight 一致）
- **交互**：拖拽缩放 x 轴 + tooltip 跨堆叠
- **API**：`/api/stats/daily?days=N` + 新增 token 拆分版 `fetch_daily_breakdown(db, *, days)`
- **D3 模块**：`d3.stack().keys(['input','output','cache_read','cache_creation']) + d3.area()`

## 4. 后端 API 契约

| Method | Path | Query | 返回 | 新增/复用 |
|---|---|---|---|---|
| GET | `/api/stats/aggregate` | `range=1d,7d,30d; dim=platform,upstream,model; top=N` | `{range, dim, since, top, total, rows:[{key,requests,input_tokens,output_tokens,cache_read_input_tokens,cache_creation_input_tokens,errors,total_tokens}]}` | 复用 v0.99 `fetch_aggregate_by_dim` |
| GET | `/api/stats/token_breakdown` | `range=7d` | `{input,output,cache_read,cache_creation,total}` | **新增** |
| GET | `/api/stats/timeseries` | `range=7d; bucket=hour,day; metric=requests,errors,total_tokens` | `{range,bucket,metric,since,items:[{ts,upstream,value}]}` | **新增** |
| GET | `/api/stats/route_heatmap` | `range=7d` | `{range,since,items:[{upstream,anthropic_n,openai_n}]}` | **新增** |
| GET | `/api/stats/sankey` | `range=7d` | 同 route_heatmap 的 items | 复用 route_heatmap |
| GET | `/api/stats/daily` | `days=1..365` | `{days,items:[{date(ISO),ts,requests,input_tokens,output_tokens,cache_read_input_tokens,cache_creation_input_tokens,errors}]}` | 复用 v0.99，扩展 days 上限 |
| GET | `/api/stats/calendar` | `year=2026` | `{year,days:N,total,items:[{date,requests,total_tokens}]}` | **新增** |
| GET | `/api/upstreams` | (复用) | `by_upstream` 配额数据 | 复用 |

### 路由顺序
`/stats/*` 具体路径全部在 `/stats/{platform}` 之前注册（避免 path-param 抢路由），
新增的 `/stats/token_breakdown` 等同理。

### 错误结构
非法 `range/bucket/metric/dim` 返回 `{error, valid:[...]}` HTTP 200（与 v0.99 一致），
前端按 error 字段分支处理。

## 5. 前端 D3.js 集成

### 5.1 引入方式
**vendor/d3.v7.min.js** 单独下载（与 chart.js 同目录），体积 ~280 KB minified。
pywebview 在 `file://` 下加载本地 vendor 脚本没有 CORS/网络问题，WebView2 直接解析。

index.html 在 chart.js 之前加载 d3：
```html
<script src="vendor/d3.v7.min.js"></script>
<script src="vendor/chart.umd.min.js"></script>
<script src="app.js?v=DATE"></script>
```

### 5.2 命名空间
所有 D3 图共享模块级 `d3Stats` 对象：
```js
const d3Stats = (window.d3 && window.d3.sankey) ? {
  instances: {},  // svgId -> {svg, destroy}
  destroy(svgId) {
    if (this.instances[svgId]) {
      const root = this.instances[svgId].svg.node();
      if (root) root.parentNode.removeChild(root);
      delete this.instances[svgId];
    }
  },
  create(svgId, container, drawFn) {
    this.destroy(svgId);
    const node = document.getElementById(svgId);
    const svg = d3.select(node).append("svg").attr(...);
    drawFn(svg);
    this.instances[svgId] = { svg };
  },
} render() } ();
```

每次 `setView("stats")` / 切换 range / dim 时调 `d3Stats.destroy + d3Stats.create`
重绘（避免 D3 enter/update/exit 状态污染）。resize listener 让图表自适应。

### 5.3 主题
D3 颜色完全走 CSS 变量（`--text-primary`, `--seg-input`, `--seg-output`,
`--seg-cache`, `--accent` 等已有 token）。`d3.interpolateInferno` 渐变作为
热力图 fallback。D3 生成 SVG 元素，layout 用 flex/grid 与现有卡片一致。

### 5.4 性能
D3 渲染每次切 range/dim 都重画整图（数据规模：30 天按小时 ~720 行，按上游拆
~10 条 × 720 行 = ~7200 cell，远低于重绘阈值）。resize 防抖 100ms。

## 6. 关键取舍说明

### 6.1 不改 DB schema
**进阶 3 图里"调用时长分布"与"重试关联图"原本依赖 DB 新字段**：
- `duration_ms INTEGER`（proxy.record 写入）
- `retry_of INTEGER`（重试关联）

**本次不增加 schema**：
- 调用时长可由 `_in_flight` 内存状态读（`started_at` → 现在），但只在内存且最近
  ~30 分钟窗口，无法做"30 天调用时长直方图"。v0.100.1 先做 8 张无时长依赖的图，
  时长图留 v0.101 单独加 schema 后再做。
- 重试关联同理（当前 relay 不做重试）。

### 6.2 复用 chart.js 而非全 D3
24h 总览图表已用 chart.js 渲染，且稳定。本次 8 张图全用 D3（用户明确要求），
但保留 chart.js 给总览页用。**chart.js 与 D3 共存，无冲突**（chart.js 用自己的
canvas，D3 用 SVG）。

### 6.3 路由热力、Sankey、日历热力 = 原"进阶三图"的等价物
用户选的"替换"清单里这 3 张图：
- 路由热力图（upstream × platform）替换"上游 × 模型 热力图" —— 数据 0 改动，
  schema 已有 platform + upstream 列；
- Sankey（上游 → 平台）替换"force graph 重试关联" —— 数据 0 改动；
- 日历热力图（全年按日 token）替换"调用时长直方图" —— 数据 0 改动，
  fetch_daily 复用。

## 7. 文件改动清单

| 文件 | 改动 |
|---|---|
| `src/relay/web/vendor/d3.v7.min.js` | 新增下载 |
| `src/relay/web/index.html` | 缓存戳 bump + d3 脚本引入；统计页 section 重写为 8 卡网格 |
| `src/relay/web/styles-20260817.css` | 追加 `.stats-page` / `.stats-chart-card` / D3 主题样式 |
| `src/relay/web/app.js` | 新增 `d3Stats` 命名空间 + 8 个 render* 函数 + setView/resetStats 桥接 |
| `src/relay/tui.py` | 新增 `fetch_token_breakdown` / `fetch_timeseries` / `fetch_route_heatmap` / `fetch_calendar` 4 个函数 |
| `src/relay/routers/stats.py` | 新增 4 个路由（`/stats/token_breakdown` / `/stats/timeseries` / `/stats/route_heatmap` / `/stats/calendar`） + 扩展 `/stats/daily` 的 days 上限到 365 |
| `src/relay/gui.py` | 新增 `Api.stats_token_breakdown` / `stats_timeseries` / `stats_route_heatmap` / `stats_calendar` 4 个桥方法（与 fetch_* 直接同步） |
| `tests/test_stats.py` | 扩展 ~20 条新测试覆盖 4 个新端点（by_dim/range/since/边界/empty/非法参数） |
| `docs/development.md` | 新增 v0.100.1 章节（图表规格、API 契约、d3 集成、查询模板） |
| `PROGRESS.md` | 新增 v0.100.1 条目 |

## 8. 风险

1. **D3 文件加载**：pywebview WebView2 `file://` 加载 vendor/d3.v7.min.js 应无问题
   （chart.js 同架构已验证）；但若用户机器禁用本地脚本会 fallback 到 chart.js
   fallback 一样的"图表库未加载"提示。
2. **resize 性能**：8 张 D3 图同时 resize + 500ms tick 渲染，加上 v0.100 spotlight
   动画——单帧 < 16ms 是硬要求。先实现前 4 张图（圆环、玫瑰、趋势、热力），profiling
   后再上 Sankey + 日历（更大布局计算）。
3. **pywebview fetch()**：与 v0.99 同样的 file:// 限制——D3 数据走 pywebview 桥
   （`api.statsTimeseries(...)`），不用 `fetch()`。

## 9. 实施顺序（5 轮 PR）

**Round 1（基础设施，0.5d）**：下载 d3 + 写 `tui.fetch_token_breakdown/timeseries/route_heatmap/calendar` + 4 个 FastAPI 路由 + GUI 桥 + 测试（~20 条） + index.html 引入 d3 + 缓存戳。**无前端渲染**——只验证 API 通畅。

**Round 2（基础 4 张图，1d）**：① donut ② rose ④ 路由热力 ⑤ Sankey —— 简单布局的静态图，主要练习 d3Stats 架构。

**Round 3（趋势+日历，1d）**：③ stacked area + brushX ⑥ 日历热力。

**Round 4（仪表盘+每日 stacked，1d）**：⑦ gauge ⑧ stacked area per day。

**Round 5（文档+PROGRESS+最终回归，0.5d）**。

每轮 PR 跑 `node --check` + `pytest tests/test_stats.py -v` + 沙箱验证前端逻辑。

## 10. 用户验证清单

GUI 端到端需确认：
- 侧栏「统计」点击进入，立即渲染（不迟钝，与 v0.100 spotlight 动画一致的"无感进入"）
- 切换时间段（24h/7d/30d）—— 8 张图同步重绘，无残留 D3 状态
- 切换 dim（platform/upstream/model）—— 仅聚合图变化，其它图不动
- 悬停 tooltip 显示完整信息
- resize 浏览器窗口 —— 图自适应
- prefers-reduced-motion: reduce —— D3 动画禁用（v0.100 spotlight 动画同步）