"""v0.119：上游链接（合并统计）解析器。

目的
----
让两个或更多上游「链接」到一起，所有统计页（总览 / 统计 / 历史 / 实时）
把它们当成一个虚拟上游显示（key=A<->B<->C），但**不影响路由、活跃状态、
计费、quota 配置** —— 上游本身依然独立。

存储
----
在 ``upstreams.json`` 每个 entry 上加 ``linked_upstreams: list[str]`` 字段
（同平台 peer name 列表；自己不能在列表里）。失败回退：字段缺失或非
list → 当作空列表（与 v0.118 及之前完全兼容）。

闭包语义
--------
A+B 且 A+C ⇒ {A, B, C} 是一个组（避免重复统计）。用 union-find 实现：
按 ``linked_upstreams`` 边做并集，最后每个连通分量 = 一个虚拟组。

虚拟 cfg 字段
-------------
对每个 size>1 的连通分量，输出一个虚拟 cfg：
  ``name``:       "A <-> B <-> C"（按名字排序后 "<-> " 连接，UI 显示用）
  ``linked_names``: ["A", "B", "C"]（SQL WHERE upstream IN (...) 用）
  ``quota_5h`` / ``billing_unit`` / ``model_multipliers`` / ``allowed_models``:
                  沿用组内「第一个」成员（按名字字典序），方便 quota 计算
                  有可参照的「主」值；其它成员仅出现在 ``linked_names``。
  ``_is_virtual``: True（GUI 渲染时识别 + 加链接 chip）。
  ``_primary``:   "A"（第一个成员，方便 UI hint「沿用 A 的 quota」）。
  ``note``:       "链接上游: A, B, C"

真实 cfg 字段
-------------
不动 —— 真实 cfg 照旧出现在 ``upstreams`` 列表，UI 单独渲染「仅自己」的卡片。
虚拟 cfg 的 ``by_upstream`` / quota 是合并值，独立统计维度。

纯函数
------
本模块不读数据库、不发请求，纯 Python 列表/字典运算，可单测。
"""
from __future__ import annotations

from typing import Any


# 虚拟 cfg 用于 UI 的分隔符（前端 / 存储 key 一致用 "<->"）。
_LINK_SEP = " <-> "


def _build_union(real_cfgs: list[dict[str, Any]]) -> dict[str, str]:
    """union-find 父指针表。key=上游 name, value=根 name。

    规则：name 在 ``linked_upstreams`` 列表里出现时，把 name 与每个 peer
    union 起来。未知 peer name（列表里的 name 当前不存在）暂不创建节点
    —— 等下次 resolve（其他上游也带它）再合并不迟；这样「先存 A 才能存 B」
    的鸡生蛋顺序不会让旧配置被静默丢失。

    多个连通分量都各自成组：每个 size>1 的组 → 虚拟 cfg；size=1 → 真实
    cfg 照旧。

    返回父指针 dict（仅含 size>1 的连通分量内的节点）。
    """
    parent: dict[str, str] = {}
    cfg_by_name: dict[str, dict[str, Any]] = {c["name"]: c for c in real_cfgs if c.get("name")}

    def find(x: str) -> str:
        # 路径压缩
        root = x
        while parent.get(root, root) != root:
            root = parent[root]
        while x != root:
            p = parent[x]
            parent[x] = root
            x = p
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            # 字典序小者作根（确定性输出，方便测试）
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    for cfg in real_cfgs:
        name = cfg.get("name")
        if not name:
            continue
        # 把 name 自己也注册进 parent（即使没有 link），保证 find() 可用
        parent.setdefault(name, name)
        for peer in cfg.get("linked_upstreams") or []:
            if not isinstance(peer, str) or not peer or peer == name:
                continue
            # 即便 peer 不在 cfg_by_name 里，也照样 union —— peer 出现
            # 在另一条 cfg 的 linked_upstreams 时会自然加入。提前 union
            # 不影响根的字典序优先级。
            parent.setdefault(peer, peer)
            union(name, peer)
    return parent


def _groups_from_union(
    parent: dict[str, str],
    real_names: list[str],
) -> list[list[str]]:
    """把 union-find 父指针转成 [[name1, name2, ...], ...] 列表。

    每个连通分量按字典序排序输出（保证测试稳定）。只保留「至少包含一个
    真实 upstream」的组 —— 纯引用的孤儿节点（linked_upstreams 里写了
    不存在的 name 且该 name 没在任何 cfg 的 ``name`` 字段里）会形成空
    组，直接丢弃。
    """
    real_set = set(real_names)
    buckets: dict[str, list[str]] = {}
    for n in real_set:
        root = parent.get(n, n)
        buckets.setdefault(root, []).append(n)
    out = []
    for grp in buckets.values():
        if len(grp) >= 2:
            out.append(sorted(grp))
        elif len(grp) == 1:
            # size=1 不需要虚拟 cfg，单独走真实路径
            pass
    return out


def _make_virtual_cfg(group_names: list[str], cfg_by_name: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """构造一个虚拟 cfg dict。

    ``name`` 按字典序连成 "A <-> B <-> C"，让 UI 显示稳定（多上游谁先
    后看字典序，不看用户编辑顺序）。

    quota / billing / multipliers / allowed_models 沿用组内第一个成员
    （按名字字典序最小者），其它成员仅以 linked_names 形式参与 SQL 合并。
    """
    primary_name = group_names[0]
    primary = cfg_by_name.get(primary_name, {})
    return {
        "name": _LINK_SEP.join(group_names),
        "linked_names": list(group_names),
        "_is_virtual": True,
        "_primary": primary_name,
        "note": f"链接上游: {', '.join(group_names)}",
        # 沿用第一个成员的 quota / 计费 —— 让 quota 卡有可参照的主值。
        # 实际「used」在 fetch_* 里通过 WHERE IN (group_names) 合并。
        "quota_5h": primary.get("quota_5h"),
        "billing_unit": primary.get("billing_unit") or "count",
        "model_multipliers": dict(primary.get("model_multipliers") or {}),
        "allowed_models": list(primary.get("allowed_models") or []),
    }


def resolve_links(real_cfgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """真实 cfg 列表 → 真实 + 虚拟 cfg 列表（合并统计后）。

    返回的列表 = 原 ``real_cfgs`` 中所有不参与任何 size>1 链接组的成员
    （**保持原顺序**），加上所有 size>1 虚拟 cfg（按 ``min(group_names)``
    字典序插入）。这样调用方拿到的 ``by_upstream[name]`` 同时含真实
    upstream 和虚拟 upstream，前端识别 ``"<->" in name`` 即可区分。

    设计要点：
    - 不动真实 cfg：active / quota / allowed_models 等字段保持原样。
    - 虚拟 cfg 与真实 cfg 同构（同样含 name / quota_5h / billing_unit /
      model_multipliers / allowed_models），便于 fetch_* / 配额计算复用。
    - 多个虚拟组互不重叠（union-find 性质）。
    - 顺序稳定：同输入 → 同输出，方便测试断言。
    """
    real_names = [c["name"] for c in real_cfgs if c.get("name")]
    if not real_names:
        return []
    parent = _build_union(real_cfgs)
    groups = _groups_from_union(parent, real_names)

    # 标记哪些真实 cfg 进入了 size>1 虚拟组（出现一次以上的真实 cfg 不输出原版）
    virtual_members: set[str] = set()
    for g in groups:
        virtual_members.update(g)

    # 按字典序找每个组的「锚点」（最小名），方便稳定排序输出
    group_by_anchor = {g[0]: g for g in groups}

    out: list[dict[str, Any]] = []
    cfg_by_name = {c["name"]: c for c in real_cfgs if c.get("name")}

    # 输出顺序：原 real_cfgs 顺序遍历，size=1 真实 cfg 照原序输出；
    # size>1 虚拟 cfg 在「第一次遇到其成员」时插入一次。
    inserted_anchors: set[str] = set()
    for cfg in real_cfgs:
        n = cfg.get("name")
        if not n:
            continue
        if n in virtual_members:
            # 第一次遇到此组成员 → 插入虚拟 cfg；之后再次遇到同组成员直接跳过
            anchor = next((a for a, g in group_by_anchor.items() if n in g), None)
            if anchor and anchor not in inserted_anchors:
                out.append(_make_virtual_cfg(group_by_anchor[anchor], cfg_by_name))
                inserted_anchors.add(anchor)
        else:
            out.append(cfg)
    return out


def display_name(cfg: dict[str, Any]) -> str:
    """虚拟 cfg 显示名（"A <-> B"），真实 cfg 直接返回 name。"""
    if cfg.get("_is_virtual"):
        return cfg.get("name", "")
    return cfg.get("name", "")


def is_virtual(cfg: dict[str, Any]) -> bool:
    """识别虚拟 cfg（合并统计）。"""
    return bool(cfg.get("_is_virtual")) or "<->" in cfg.get("name", "")