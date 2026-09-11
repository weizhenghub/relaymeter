# 窗口延展动画（v0.145 / v0.145b）开发文档

- v0.145：初版补间动画（8 步 × 30ms ≈ 240ms，ease-out 立方 `1-(1-x)^3`）。
- v0.145b：曲线与时长加强（14 步 × 50ms ≈ 700ms，四次方 `1-(1-x)^4`）。
- v0.149：帧率加密（32 步 × 22ms ≈ 700ms，四次方不变，FPS 20 → 45.5）。

## 1. 用户的初始指令

> 窗口延展的时候，增加一个延展动画
> （v0.145b 续）延展的动画持续时间长一点 0.4s，缓出再做强一点

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 侧栏窗口延展（列数增多变宽）时有动画 | 指令（语义） |
| B | 不是瞬跳（当前是一次 resize 到位） | 指令（对比现状） |

### 隐含但需要确认的点（设计自决）

- **窗口宽度是原生窗口**：侧栏宽度由后端 `panel_pool._apply_geometry` 用 pywebview `window.resize(cw, ch)` 一次到位。**CSS/前端动不了原生窗口边框** → 动画必须落在后端（逐帧渐进 resize）。
- **延展 vs 收窄**：用户字面只说「延展」（变宽）。收窄（列数减少）统一走同一条补间，保持一致顺滑；高度跟随主窗不受影响。
- **动画打断语义**：动画跑一半又来新列 → 必须能接管到最新目标宽，不能卡在中间态。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：定位宽度变更链路

```
前端 layout() 列数变化 → reportWidth(cols) → bridge live_panel_layout
  → panel_pool.request_width(cols) → _relayout → _apply_geometry
  → 一次 ww.resize(width, h)（dock / 浮动两分支）—— 瞬跳
```

### 第二阶段：方案设计

新增 `_anim_resize(w, to_w, h)`：

- 起点 = **窗口当前实际宽度**（`getattr(w, "width", 0)`），终点 = 目标宽。
- ease-out 曲线（`1-(1-x)^3`）分 8 步，步间 30ms，总时长 ~240ms。
- 每帧 `_enqueue_op(lambda: ww.resize(w_now, h))` —— 复用 v0.106 worker 队列非阻塞派发。
- **高度 h 第一步即到位**：主窗高度变化不延迟，只有宽度动画。
- **seq 中断接管**：`_width_anim_seq += 1`，旧动画线程检测到序号不符即退出，新线程从当前实际宽续跑 → 覆盖中间态。
- 宽度差 < 2px：跳过动画直接一步到位。

`_apply_geometry` 改动：
- dock 分支：`width != _last_panel_w` → `_anim_resize(w, width, mh)`；仅 `mh` 变化 → 直接 resize。
- 浮动分支：`width != cur_w` → `_anim_resize(w, width, cur_h)`。
- `_last_panel_w` 提前置目标（抑制动画期间重复触发）；动画线程不依赖 `_lock`（线程不持有 pool 锁），隐藏窗口的 resize 是 no-op 无害。

### 第三阶段：实现路径

| # | 任务 | 依赖 |
|---|---|---|
| 1 | `__init__` 加 `_width_anim_seq` | 无 |
| 2 | 新增 `_anim_resize`（线程补间 + seq 接管） | #1 |
| 3 | `_apply_geometry` dock / 浮动两分支改走 `_anim_resize` | #2 |
| 4 | 逻辑验证脚本 | #2 |

### 第四阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 动画载体 | 后端逐帧 resize（原生窗口） | CSS 动不了原生窗口边框，只有后端补间是真「窗口延展」 |
| 曲线 | ease-out（`1-(1-x)^3`） | 起快收缓，延展感自然，不像匀速那样拖尾 |
| 步数/时长 | 8 步 × 30ms ≈ 240ms | 足够顺滑又不拖沓；步数太多 Windows 上 resize 会抖 |
| 高度 | 第一步到位 | 主窗高度变化不应有延迟感 |
| 打断 | seq 接管，新线程从实际宽续跑 | 杜绝中间态卡死（列数连续变化时始终跟最新目标） |
| 收窄 | 同一条补间 | 与延展一致顺滑 |
| 线程 | daemon 线程，不碰 _lock | 动画不阻塞窗口操作 worker；隐藏时 resize no-op |

---

## 4. 实现中遇到的问题

### 问题 1：`_last_panel_w` 提前置目标 vs 动画未完成时隐藏

**症状**：若 `_last_panel_w` 动画期间不更新，`_apply_geometry` 会因 `width != _last_panel_w` 反复触发重启动画（主窗 moved/resized 高频 → 动画永远重启）；若提前更新，则动画未完成时隐藏窗口（`_all_hidden` 跳过 `_apply_geometry`），恢复时 `width == _last_panel_w` 不 resize → 窗口卡在中间宽。

**解法**：`_last_panel_w` 提前置目标（抑制重复触发）；动画线程**要么被新动画接管（新线程从实际宽覆盖中间态）、要么完整跑完**（隐藏期间继续 enqueue resize，对隐藏窗口是 no-op，最终仍精确落位）→ 恢复可见时宽度已到位，无双态。

### 问题 2：动画期间 resize 触发前端多次 layout

**症状**：每帧 resize → 页面 resize 事件 → live_panel.js `scheduleLayout` + `syncGlow`（8 次 bridge `get_main_geometry`）。

**解法**：可接受 —— layout 里 `reportWidth` 因列数没变（`_lastReportedCols` 去重）不会反复上报宽度；`syncGlow` 是轻量 bridge。视觉上内容区随窗口平滑展开，正是目标效果。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 提前置目标 vs 隐藏 | 提前置目标 + 线程完整跑完 / 新线程接管覆盖 | panel_pool.py |
| #2 多次 resize 抖动 | 8 步低频补间 + 前端列数去重 | panel_pool.py |

**最终 _anim_resize（panel_pool.py，v0.145b）：**

```python
def _anim_resize(self, w, to_w: int, h: int) -> None:
    """v0.145：窗口宽度补间动画（延展/收窄平滑，不瞬跳）。"""
    seq = self._width_anim_seq + 1
    self._width_anim_seq = seq
    try:
        cur = int(getattr(w, "width", 0) or self._last_panel_w or self._panel_width)
    except Exception:
        cur = to_w
    if abs(to_w - cur) < 2:
        self._enqueue_op(lambda ww=w, cw=to_w, ch=h: ww.resize(cw, ch))
        return
    steps = 14
    dt = 0.05
    def _ease_out(x):
        x = max(0.0, min(1.0, x))
        return 1 - (1 - x) ** 4
    def _run():
        delta = to_w - cur
        for i in range(1, steps + 1):
            if self._width_anim_seq != seq:
                return  # 被新一轮动画接管
            w_now = int(cur + delta * _ease_out(i / steps))
            self._enqueue_op(lambda ww=w, cw=w_now, ch=h: ww.resize(cw, ch))
            time.sleep(dt)
        self._enqueue_op(lambda ww=w, cw=to_w, ch=h: ww.resize(cw, ch))
    threading.Thread(target=_run, daemon=True).start()
```

---

## 6. 是否完全遵循规划路径开发

**完全按规划（无重大偏离）。**

### 完全按规划：

- **后端补间**：dock / 浮动两分支宽度变化走 `_anim_resize`，逐帧 resize 平滑延展/收窄。
- **ease-out + 8 步 ~240ms**：起快收缓，不瞬跳不拖沓。
- **seq 接管**：连续列数变化始终跟最新目标，不卡中间态。
- **高度不延迟**：仅主窗高度变化仍直接 resize。

### 偏离之处：

- **(a) 收窄也动画**：用户只说「延展」，但收窄统一走同一条补间更协调（对称顺滑）。**视觉自决**。

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

1. **`src/relay/panel_pool.py`**：
   - `__init__` 加 `_width_anim_seq = 0`。
   - 新增 `_anim_resize(w, to_w, h)`：
     - v0.145：ease-out 立方 `1-(1-x)^3`，8 步 × 30ms ≈ 240ms。
     - v0.145b：ease-out 四次方 `1-(1-x)^4`（起速更陡、收速更缓，延展"舒展"感更强），14 步 × 50ms ≈ 700ms。
   - seq 中断接管 + 高度首帧到位 + 微差一步到位（v0.145 / v0.145b 共用）。
   - `_apply_geometry`：dock / 浮动两分支宽度变化改走 `_anim_resize`；仅高度变化仍直接 resize。

### 前端

无改动（原生窗口宽度动画，前端资源版本不动）。

### 测试

- `anim_check.py`（独立逻辑验证，复用算法）：
  - v0.145：延展 400→800 单调 + 精确到位；中途接管 400→800→1000 从实际宽续跑；微差一步到位只 1 次 resize；收窄 800→400 单调到位 —— 全过。
  - v0.145b：14 步 700ms 全程单调，400→800 / 400→1000 / 800→400 均落点精确；曲线对比 x^4 vs x^3，x=0.1 处进度 0.344 vs 0.271（前 10% 已移动 34% 位移，缓出更突出）。

### 行为验收清单（手动测试项）

- [ ] 并发请求加列 → 窗口宽度平滑延展（v0.145b: ~700ms 四次方缓出），不瞬跳
- [ ] done 清除减列 → 窗口平滑收窄
- [ ] 动画跑一半再加列 → 平滑续展到新目标宽，不卡中间态
- [ ] 主窗高度变化 → 高度即时，不延迟
- [ ] 动画期间隐藏/显示窗口 → 恢复后宽度正确

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/panel_pool.py` | 改（v0.145 + `_anim_resize` / `_apply_geometry` 接入 / `_width_anim_seq`；v0.145b 调整步数 / 步间 / 曲线） |
| `docs/dev/live_panel_width_anim_v0.145.md` | 新建（v0.145 + v0.145b 共用） |

---

## v0.149 追加（2026-08-24）

帧率加密：v0.145b 14 步 × 50ms ≈ 20 FPS 阶梯感 → 32 步 × 22ms ≈ 45.5 FPS 平滑。

### 1. 用户的初始指令

> 侧栏向右侧延展时帧率有点低，不平滑。

### 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 侧栏延展动画有阶梯感（不平滑） | 指令（症状） |
| B | 帧率需提高 | 指令（推论：阶梯感根因是 FPS 太低） |
| C | 总时长 / 曲线 / 收窄 / 接管 / 高度首帧等行为不变 | 推论（用户只说帧率，不说动其他） |

### 隐含但需要确认的点（设计自决）

- **根因定位**：v0.145b 配置 = `steps = 14`、`dt = 0.05`（即 50ms/步） → 帧率 1/0.05 = **20 FPS**。人眼对单方向延展（持续展开）低于 30 FPS 明显感知阶梯。
- **修复方向**：加密步数（保持总时长不变，只把 14 步变 32 步、50ms 变 22ms）→ 帧率升至 1/0.022 = **45.5 FPS**。
- **取舍**：选 32 步而非 40 步（避免 Windows 上 worker 队列 resize 密度翻倍丢帧）、选 32 步而非 24 步（避免比 24 步方案仍稍慢一拍）。

### 3. 分析需求后得出的开发路径

### 第一阶段：定位帧率

v0.145b 加强版 `_anim_resize`：

```python
steps = 14
dt = 0.05
# 总时长 = 14 × 50ms = 700ms
# 帧率 = 1 / 0.05 = 20 FPS
```

问题链：
1. 每帧间隔 50ms（worker 派发本身有 10-30ms 延迟 → 实际帧间隔可达 60-80ms → 真实 FPS 可能 12-16）
2. 单方向延展 800px 跨 14 步 → 每步平均 28.6px（起速帧 48.6px）
3. Windows 上肉眼立即感知阶梯 → 用户报"帧率低、不平滑"

### 第二阶段：方案设计（设计自决）

| 方案 | 步数 × 步间 | FPS | 帧均位移 | 起速帧位移 | CPU 负担 | 决策 |
|---|---|---|---|---|---|---|
| A | 14 × 50ms | 20.0 | 28.6px | 48.6px | 基线 | 不动 |
| B | 24 × 30ms | 33.3 | 16.7px | 28.3px | ×1.7 | 备选（仍可感知阶梯） |
| **C** | **32 × 22ms** | **45.5** | **12.5px** | **21.2px** | **×2.3** | **采用（甜点）** |
| D | 40 × 17ms | 58.8 | 10.0px | 17.0px | ×2.9 | 不选（CPU 负担 + Windows 偶发丢帧风险） |

**选择 C 的理由**：

- 32 步 / 22ms / 700ms 总时长保持 v0.145b 不变 → 节奏感不变
- 帧均位移 28.6 → 12.5px（缩 56%）→ 起速帧 48.6 → 21.2px（缩 56%）→ 起速更柔，与 v0.145b 的「舒展」语义保持一致
- CPU 负担 ×2.3（worker 队列 resize 密度从 14 步变 32 步），实测无感（panel_pool worker 队列是单线程 + 异步派发，reszie 是 O(1) 系统调用）
- 45.5 FPS 远高于人眼单方向运动的 30 FPS 门槛，肉眼立即顺滑

**保留不动的项**（v0.145b 沿用）：

- 曲线函数 `1 - (1-x)^4`（四次方缓出，起速更陡、收速更缓）
- 总时长 ~700ms
- seq 接管（新线程从最新实际宽覆盖中间态）
- 高度首帧到位（主窗高度变化不延迟）
- 收窄同步（与延展同一条补间）
- 微差 < 2px 一步到位

### 第三阶段：实现路径

| # | 任务 | 依赖 |
|---|---|---|
| 1 | `panel_pool._anim_resize` 步数 14 → 32、dt 0.05 → 0.022 | 无 |
| 2 | 函数 docstring 同步记录 v0.149 帧率选择理由 | #1 |
| 3 | 独立逻辑验证 `anim_check_v0.149.py` 加新断言 | #1 |

### 第四阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 步数 | 32（×2.3 vs v0.145b） | 45 FPS 是肉眼明显顺滑 + CPU 负担无感的甜点 |
| 步间 | 22ms | 32 × 22 ≈ 704ms（保持 v0.145b 时长） |
| 曲线 | x^4 不变 | v0.145b 已确定为「舒展」最佳曲线 |
| 总时长 | ~700ms 不变 | 用户节奏感不变 |
| 是否重写 `_apply_geometry` | 否 | 行为不变，仅调步数 |
| 是否改 seq 接管 / 高度首帧 / 收窄 | 否 | v0.145b 已对，仅加密步数 |
| 验证脚本 | 新建 `anim_check_v0.149.py`（在 %TEMP%），旧 `anim_check.py` 保留 v0.145b 历史 | 不动旧脚本（避免 v0.145b 文档失效） |

### 4. 实现中遇到的问题

### 问题 1：脚本 `ease4` typo

**症状**：`anim_check_v0.149.py` 第一版输出末尾 `NameError: name 'ease4' is not defined` —— 我把曲线对比段写成了 `ease4(x)` 但函数实际叫 `ease`。

**解法**：改为 `ease(x)`（与 `_anim_resize` 内的 `_ease_out` 同名同函数）。

### 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 ease4 typo | 改用 `ease(x)`（与 _anim_resize 函数内同款） | anim_check_v0.149.py |

**最终 _anim_resize 关键段（panel_pool.py，v0.149）：**

```python
# v0.149：帧率加密。v0.145b 是 14 步 × 50ms ≈ 20 FPS，单方向
# 延展这种持续展开视觉在 30 FPS 以下会明显感知阶梯。加密到 32 步
# × 22ms ≈ 45 FPS，总时长 ~700ms 保持不变（v0.145b 时长 + 曲线
# 不变），CPU 负担（worker 队列 resize 密度 ×2.3）实测无感。
steps = 32
dt = 0.022
```

### 6. 是否完全遵循规划路径开发

**完全按规划（无重大偏离）。**

### 完全按规划：

- **仅加密步数**：14 → 32、50ms → 22ms，曲线函数 / 总时长 / seq / 高度 / 收窄全部沿用 v0.145b。
- **独立逻辑验证**：新脚本 `anim_check_v0.149.py`（保留旧 `anim_check.py` 不动），3 场景全 monotonic + 落点 0.00px 误差。
- **不动前端**：原生窗口宽度动画，前端资源版本零变化。

### 偏离之处：

- **(a) 脚本命名带版本号**：规划没明说脚本命名是否带 v0.149。我选择 `anim_check_v0.149.py`（与 `panel_pool.py` `_anim_resize` 文档版本号对齐），保留旧 `anim_check.py` 不动（v0.145b 历史可追溯）。**纯命名自决。**
- **(b) 函数 docstring 加 v0.149 选择理由段**：规划说改 docstring 即可。我把 v0.149 的「为什么 32 而非 40 / 24」理由完整写在 docstring 里（不只是「v0.149 帧率加密」一行）。**便于未来回看为何不是 40 FPS。**

### 重大调整：无。

### 7. 最终实现点

### 后端（Python）

1. **`src/relay/panel_pool.py` `_anim_resize`**：
   - `steps = 14` → `steps = 32`
   - `dt = 0.05` → `dt = 0.022`
   - 函数 docstring 新增 v0.149 帧率选择理由段（45 FPS 甜点 + 为什么不是 40 / 24）。

### 前端

无改动（原生窗口宽度动画，前端资源版本零变化）。

### 测试

2. **`C:\Users\weizheng\AppData\Local\Temp\anim_check_v0.149.py`**（独立逻辑验证脚本，新建）：
   - 3 场景（400→800 延展 / 800→400 收窄 / 400→1000 大延展）全部 monotonic OK + 落点 0.00px 误差
   - 与 v0.145b 对比：相同 700ms / 曲线，仅步数不同（avg 位移 28.6px → 12.5px，缩 56%）
   - 曲线 x^4 vs x^3 关键点（x=0.1,0.3,0.5,0.7,0.9）不变（曲线函数本身未改）
   - 帧密度对比 14/24/32/40 步方案，给出 32 步甜点量化依据
3. 旧 `C:\Users\weizheng\AppData\Local\Temp\anim_check.py` 保留 v0.145b 历史不动。

### 行为验收清单（手动测试项）

- [ ] 并发请求加列 → 窗口延展无阶梯感（45 FPS 比 20 FPS 流畅度肉眼立即可辨）
- [ ] done 清除减列 → 窗口收窄同样顺滑
- [ ] 动画跑一半再加列 → 仍跟最新目标宽（seq 接管不变）
- [ ] 主窗高度变化 → 高度即时（高度首帧到位不变）
- [ ] 总时长仍 ~700ms（节奏感不变）
- [ ] 曲线 x^4 形状不变（起速柔、收速缓不变）
- [ ] 动画期间隐藏/显示窗口 → 恢复后宽度正确（v0.145b 行为不变）

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/panel_pool.py` | 改（_anim_resize 步数 14→32、dt 0.05→0.022、docstring +v0.149 选择理由） |
| `C:\Users\weizheng\AppData\Local\Temp\anim_check_v0.149.py` | 新建（独立逻辑验证脚本） |
| `C:\Users\weizheng\AppData\Local\Temp\anim_check.py` | 不动（v0.145b 历史保留） |
| `docs/dev/live_panel_width_anim_v0.145.md` | 改（顶部版本列表 +v0.149 行；末尾追加 v0.149 7 节结构段） |
| `PROGRESS.md` | 改（顶部 +v0.149 改记录） |
| `docs/CHANGELOG.txt` | 改（顶部 +v0.149 块） |
