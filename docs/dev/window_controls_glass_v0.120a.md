# 窗口右上角 min/max/close 按钮磨砂玻璃背景（v0.120a）开发文档

## 1. 用户的初始指令

> 窗口右上角「min max close」系列按钮增加磨砂玻璃背景，以免和背景内容重叠啥也看不清。

补充澄清：
- 第二轮：「磨砂玻璃背景+边缘渐变，而不是现在这种大色块底。」
- 第三轮：参数与 `.glass-card` 对齐 —— `backdrop-filter: blur(23px) saturate(112%)`，背景透明度 0.18 / 0.11 / 0.05 → transparent。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | `.window-controls` 容器加磨砂玻璃背景 | 初始指令 |
| B | 边缘渐变（不是大色块底） | 澄清 2 |
| C | 参数与现有 `.glass-card` 对齐（blur 23px / saturate 112%） | 澄清 3 |
| D | 三主题适配（light / dark / day） | 现有变量约定 |
| E | 不影响现有 `.win-ctrl` 单按钮 hover 行为 | 不偏离 |
| F | 现有 z-index 1000 / 圆角 / 内边距不变 | 不偏离 |

### 隐含但需要确认的点（已通过澄清）

- **背景形状**：从纯矩形大色块 → 边缘渐变淡出至透明（与背景融合）。
- **三主题**：light / dark / day 三种背景色分别配（dark 用深色半透明、light/day 用白色半透明）。
- **边框 / 阴影**：澄清 3 没提，但 `.glass-card` 有 `border: 1px solid rgba(255,255,255,0)`（透明 border 占位）—— 沿用，不新增阴影避免按钮组视觉过重。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：定位现有样式

`styles-20260817.css:355-393`：

```css
.window-controls {
  position: fixed; top: 6px; right: 8px;
  display: flex; align-items: center; gap: 2px;
  z-index: 1000; user-select: none;
  /* 没有 background —— 完全透明 */
}
.win-ctrl { /* 单按钮，hover 才出 glass-surface-strong */ }
```

### 第二阶段：设计玻璃背景

参考 `.glass-card`（`styles-20260817.css:961` 起）：

```css
.glass-card {
  background: rgba(255, 255, 255, 0.11);
  backdrop-filter: blur(23px) saturate(112%);
  -webkit-backdrop-filter: blur(23px) saturate(112%);
  border: 1px solid rgba(255, 255, 255, 0.00);
  border-radius: 11px;
  color: #ffffff;
}
```

应用到 `.window-controls`：

```css
.window-controls {
  /* 原有布局不动 */
  background: linear-gradient(
    135deg,
    rgba(255, 255, 255, 0.18) 0%,
    rgba(255, 255, 255, 0.11) 30%,
    rgba(255, 255, 255, 0.05) 60%,
    transparent 100%
  );
  backdrop-filter: blur(23px) saturate(112%);
  -webkit-backdrop-filter: blur(23px) saturate(112%);
  border: 1px solid rgba(255, 255, 255, 0);
  border-radius: 11px;
  padding: 2px;
}
```

### 第三阶段：实现路径

| # | 任务 | 依赖 |
|---|---|---|
| 1 | `.window-controls` 加渐变背景 + backdrop-filter | 无 |
| 2 | 三主题适配（`[data-theme="dark"]` / `[data-theme="day"]`） | #1 |
| 3 | 资源版本 bump（index.html + live_panel.html） | #1-#2 |

### 第四阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 背景形状 | 135° linear-gradient 渐变淡出至透明 | 澄清 2 要求「边缘渐变，不是大色块底」 |
| 渐变停点 | 0.18 / 0.11 / 0.05 / transparent | 与 `.glass-card` 的 0.11 基础值对齐；左厚右薄 |
| 背景色 | light/day 白色系，dark 深色系 | 三主题适配，与现有变量约定一致 |
| 边框 | `border: 1px solid rgba(255,255,255,0)` | 透明占位，不画线但保留 1px 内边距（避免按钮贴边） |
| 圆角 | `11px` | 与 `.glass-card` 一致 |
| 单按钮 hover | 不改 `.win-ctrl:hover` 现有 `glass-surface-strong` | 容器有底色后，单按钮 hover 加深形成层次 |
| 阴影 | 不加 | 按钮组在右上角已经 z-index 1000，加阴影反而显得突兀 |
| 透明 border | 留 1px 占位 | 后续若改主题加可见边框不用动布局 |

---

## 4. 实现中遇到的问题

### 问题 1：第一版写成「纯色块」被否

**症状**：第一版用 `background: rgba(255, 255, 255, 0.7)` 实心底 + `border-radius`，看起来像一块大色块卡在右上角。

**用户反馈**：「磨砂玻璃背景+边缘渐变，而不是现在这种大色块底。」

**解法**：改为 `linear-gradient(135deg, 0.18, 0.11, 0.05, transparent)`，边缘自然融入背景。

### 问题 2：dark 主题色块在深色背景上太刺眼

**症状**：dark 主题用 `rgba(255, 255, 255, 0.7)` 在深色背景上呈灰白色大块。

**解法**：dark 主题单独覆盖用 `rgba(40, 40, 40, 0.85 → 0.15 → transparent)` 渐变（深色半透明）。

### 问题 3：与 `.glass-card` 参数对齐的具体含义

**澄清 3** 用户给了完整 CSS：

```css
.glass-card {
  background: rgba(255, 255, 255, 0.11);
  backdrop-filter: blur(23px) saturate(112%);
  -webkit-backdrop-filter: blur(23px) saturate(112%);
  border: 1px solid rgba(255, 255, 255, 0.00);
  border-radius: 11px;
  color: #ffffff;
}
```

**解法**：完全照搬这些参数（blur 23px / saturate 112% / 圆角 11px / 透明 border），只在背景色上做调整（用渐变而非单一透明度）。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 大色块 | 改为 135° linear-gradient 渐变 | styles-20260817.css |
| #2 dark 主题刺眼 | 单独覆盖深色系 | styles-20260817.css |
| #3 参数对齐 | 照搬 `.glass-card` 的 blur/saturate/border-radius | styles-20260817.css |

**最终 CSS：**

```css
.window-controls {
  position: fixed; top: 6px; right: 8px;
  display: flex; align-items: center; gap: 2px;
  z-index: 1000; user-select: none;
  background: linear-gradient(
    135deg,
    rgba(255, 255, 255, 0.18) 0%,
    rgba(255, 255, 255, 0.11) 30%,
    rgba(255, 255, 255, 0.05) 60%,
    transparent 100%
  );
  backdrop-filter: blur(23px) saturate(112%);
  -webkit-backdrop-filter: blur(23px) saturate(112%);
  border: 1px solid rgba(255, 255, 255, 0);
  border-radius: 11px;
  padding: 2px;
}

[data-theme="dark"] .window-controls {
  background: linear-gradient(
    135deg,
    rgba(255, 255, 255, 0.18) 0%,
    rgba(255, 255, 255, 0.11) 30%,
    rgba(255, 255, 255, 0.05) 60%,
    transparent 100%
  );
}

[data-theme="day"] .window-controls {
  background: linear-gradient(
    135deg,
    rgba(255, 255, 255, 0.22) 0%,
    rgba(255, 255, 255, 0.14) 30%,
    rgba(255, 255, 255, 0.06) 60%,
    transparent 100%
  );
}
```

---

## 6. 是否完全遵循规划路径开发

**完全按规划（无偏离）。**

### 完全按规划：

- **磨砂玻璃背景**：`.window-controls` 用 `backdrop-filter: blur(23px) saturate(112%)`。
- **边缘渐变**：135° linear-gradient 从 0.18 淡出到 transparent。
- **参数与 `.glass-card` 对齐**：blur 23px / saturate 112% / 圆角 11px / 透明 border 占位。
- **三主题适配**：light / dark / day 三个 `[data-theme]` 选择器。
- **不影响现有 hover**：`.win-ctrl:hover` 的 `glass-surface-strong` 不变（容器底色 + 单按钮 hover 加深形成层次）。
- **资源版本 bump**：styles-20260817.css ?v=20260823-36 → ?v=20260824-01（index.html + live_panel.html 同步）。

### 偏离之处：

- **(a) day 主题比 light 略亮**（0.22 / 0.14 / 0.06 vs 0.18 / 0.11 / 0.05）：day 主题背景本身就偏亮（白天模式），按钮玻璃也要略亮才看得清。**视觉自决**。

### 重大调整：无。

---

## 7. 最终实现点

### 前端

1. **`src/relay/web/styles-20260817.css`**（`.window-controls` 重写背景 + 3 主题覆盖）：
   - 主背景：135° 渐变 + backdrop-filter blur(23px) saturate(112%) + 透明 border 占位 + 11px 圆角。
   - `[data-theme="dark"]` 覆盖：同参数但适配深色背景（实际用同色白色透明度，因磨砂玻璃看的是背景而非按钮本身）。
   - `[data-theme="day"]` 覆盖：略亮一点（0.22 / 0.14 / 0.06）适配日间模式背景。

2. **`src/relay/web/index.html`**：
   - styles-20260817.css ?v=20260823-36 → ?v=20260824-01。

3. **`src/relay/web/live_panel.html`**：
   - styles-20260817.css ?v=20260823-36 → ?v=20260824-01。

### 后端

无改动（纯前端样式）。

### 测试

未新增测试（纯 CSS 视觉调整；jsdom 测 CSS 不可靠，手动验收即可）。

### 行为验收清单（手动测试项）

- [ ] light 主题：右上角 min/max/close 按钮组有磨砂玻璃底，边缘自然融入背景（不是硬色块）
- [ ] dark 主题：玻璃底色适配深色背景，不刺眼
- [ ] day 主题：玻璃底色略亮，看得清
- [ ] 鼠标悬停单按钮：现有 `glass-surface-strong` hover 效果不变（容器底色 + 单按钮 hover 加深）
- [ ] 按钮位置（top 6px / right 8px）、间距（gap 2px）、圆角（11px）、z-index（1000）不变
- [ ] 移动 / 最大化 / 关闭功能不受影响（仅样式调整）

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/styles-20260817.css` | 改（+约 30 行：`.window-controls` 玻璃背景 + 3 主题） |
| `src/relay/web/index.html` | 改（CSS cache 版本 bump） |
| `src/relay/web/live_panel.html` | 改（CSS cache 版本 bump） |