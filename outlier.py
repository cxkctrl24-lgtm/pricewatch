# -*- coding: utf-8 -*-
"""价格离群检测 v2：在**同一分类内部**找"过于离谱"的价格，分三级处置。

为什么必须分类内比较：
  全站比价会说"¥69 的 DDR4 8G 很便宜"，但和 ¥1200 的 DDR4 16G 比毫无意义。
  只有在「DDR4 8G 台式」这个同档位里，¥69 才算异常（正常 ¥150-300）。

三级处置：
  reject   → 直接不收（丢进异常清单，不入库）
  review   → 入库但标记为"异常"，单独列表展示
  ok       → 正常收录

判定依据（按稳健性排序）：
  1. **IQR 法**（首选）：Q1-1.5*IQR ~ Q3+1.5*IQR 之外算离群
     样本 >= 4 件才能用，对多峰分布也稳
  2. **中位数倍数**：低于中位数 0.4 倍 或 高于 2.5 倍
     样本 2-3 件时退化到这条
  3. **分类基准价**：内置行业价目，样本 < 2 件时用它兜底
"""
import statistics

# 内置基准价（元）。样本不足时用来判断"离谱"。
# 区间是「一手全新」的常见成交价，二手通常是 40%–60%。
BASELINE = {
    # DDR5 台式
    ("DDR5", 8, 1, "台式"): (260, 700),
    ("DDR5", 16, 1, "台式"): (450, 1500),
    ("DDR5", 32, 1, "台式"): (900, 3200),
    ("DDR5", 16, 2, "台式"): (900, 3600),     # 16G×2
    ("DDR5", 32, 2, "台式"): (1800, 7000),    # 32G×2
    # DDR5 笔记本
    ("DDR5", 8, 1, "笔记本"): (200, 650),
    ("DDR5", 16, 1, "笔记本"): (400, 1400),
    ("DDR5", 32, 1, "笔记本"): (800, 2800),
    ("DDR5", 16, 2, "笔记本"): (800, 2800),
    # DDR4 台式
    ("DDR4", 4, 1, "台式"): (40, 180),
    ("DDR4", 8, 1, "台式"): (70, 320),
    ("DDR4", 16, 1, "台式"): (140, 700),
    ("DDR4", 32, 1, "台式"): (350, 1500),
    ("DDR4", 8, 2, "台式"): (140, 620),
    ("DDR4", 16, 2, "台式"): (280, 1300),
    # DDR4 笔记本
    ("DDR4", 4, 1, "笔记本"): (40, 160),
    ("DDR4", 8, 1, "笔记本"): (70, 300),
    ("DDR4", 16, 1, "笔记本"): (150, 650),
    ("DDR4", 8, 2, "笔记本"): (150, 600),
    # 显卡（全新一手）
    ("RTX", "5060", 8): (1800, 3200),
    ("RTX", "5060", 12): (2200, 3800),
    ("RTX", "5060 Ti", 8): (2200, 3600),
    ("RTX", "5060 Ti", 16): (2600, 4200),
    ("RTX", "5070", 12): (3600, 5800),
    ("RTX", "5070 Ti", 16): (5200, 8200),
    ("RTX", "5080", 16): (7800, 13500),
    ("RTX", "5090", 32): (15000, 32000),
}

# 二手通常是同档一手价的这个比例区间
USED_RATIO = (0.30, 0.75)


def _valid_pair(r):
    """基准表里的 (low, high) 可能有个别缺项，只接受两个都是正数的。"""
    if not r:
        return None
    try:
        lo, hi = float(r[0]), float(r[1])
    except (TypeError, ValueError, IndexError):
        return None
    if lo <= 0 or hi <= 0:
        return None
    return lo, hi


def _baseline(spec):
    """按规格查内置基准价。返回 (low, high) 或 None。"""
    if not spec:
        return None
    if spec.get("gpu"):
        key = ("RTX", spec["gpu"].replace("RTX ", ""), spec.get("vram"))
        return _valid_pair(BASELINE.get(key))
    gen = spec.get("gen")
    cap = spec.get("cap")
    cnt = spec.get("count") or 1
    mt = spec.get("mem_type") or "通用"
    if not gen or not cap:
        return None
    # 通用型先试台式，再试笔记本
    for t in ([mt] if mt != "通用" else ["台式", "笔记本"]):
        r = _valid_pair(BASELINE.get((gen, cap, cnt, t)))
        if r:
            return r
    return None


def _iqr_bounds(prices):
    """IQR 上下界。样本 < 4 返回 None。

    先清洗：None / 非数字 / <=0 的价格全部丢弃。
    采集侧偶尔会漏掉价格（解析不到"¥"），混进来会让 median/排序直接崩。
    """
    clean = []
    for p in prices or ():
        try:
            v = float(p)
        except (TypeError, ValueError):
            continue
        if v > 0:
            clean.append(v)
    if len(clean) < 4:
        return None
    s = sorted(clean)
    n = len(s)
    q1 = statistics.median(s[: n // 2])
    q3 = statistics.median(s[(n + 1) // 2:])
    iqr = q3 - q1
    if iqr <= 0:
        # 全一样或太少样，用中位数 ±60%
        med = statistics.median(s)
        return med * 0.4, med * 2.5
    return q1 - 1.5 * iqr, q3 + 1.5 * iqr


def judge(price, spec, peers, is_used=False):
    """判断单件价格是否离谱。

    peers: 同分类**一手**商品的价格列表（判断基准）
    返回 (等级, 理由, 参考低价, 参考高价)
      等级 = ok / review / reject
    """
    if not price or price <= 0:
        return "reject", "价格无效", None, None

    # peers 里可能混进 None / 空串（解析不到价格），统一清成干净的浮点列表
    clean_peers = []
    for p in (peers or ()):
        try:
            v = float(p)
        except (TypeError, ValueError):
            continue
        if v > 0:
            clean_peers.append(v)
    peers = clean_peers

    # 二手用一手价的比例区间判定
    if is_used:
        return _judge_used(price, spec, peers)

    # ---------- 一手 ----------
    bounds = _iqr_bounds(peers) if peers else None
    if bounds:
        lo, hi = bounds
        if price < lo:
            dev = (lo - price) / lo * 100 if lo > 0 else 999
            level = "reject" if dev > 55 else "review"
            return level, f"低于同档正常下界 ¥{lo:.0f}（低 {dev:.0f}%）", lo, hi
        if price > hi:
            dev = (price - hi) / hi * 100 if hi > 0 else 999
            # 高出上界 4 倍以上几乎肯定是套装/多件/写错单位
            level = "review" if dev < 300 else "review"
            return level, f"高于同档正常上界 ¥{hi:.0f}（高 {dev:.0f}%）", lo, hi
        return "ok", "", lo, hi

    # 样本不足 → 用内置基准
    base = _baseline(spec)
    if base:
        lo, hi = base
        if price < lo * 0.55:
            return "reject", f"远低于行业基准 ¥{lo}-¥{hi}（仅 {price/hi*100:.0f}%）", lo, hi
        if price < lo:
            return "review", f"略低于行业基准 ¥{lo}-¥{hi}", lo, hi
        if price > hi * 3:
            return "review", f"远高于行业基准 ¥{lo}-¥{hi}", lo, hi
        return "ok", "", lo, hi

    # 什么都没得比（样本 1-3 且规格不全）→ 只做极粗的兜底
    if peers and len(peers) >= 2:
        med = statistics.median(peers)
        if price < med * 0.40:
            return "reject", f"低于同档中位数 ¥{med:.0f} 的 40%", None, None
        if price > med * 3:
            return "review", f"高于同档中位数 ¥{med:.0f} 的 3 倍", None, None
    return "ok", "", None, None


def _judge_used(price, spec, peers):
    """二手价判定：只抓"低到不像真内存"的，偏贵的不动 ——
    二手偏贵是市场行为（老型号、小容量反而稀缺），不该标异常。"""
    # 基准优先用同档二手自己的中位数（如果样本够）
    used_median = statistics.median(peers) if peers and len(peers) >= 3 else None
    base = _baseline(spec)

    # 只判"过于便宜"：低于同档二手价 35%，或低于一手基准的 15%
    if used_median and used_median > 0 and price < used_median * 0.35:
        return "reject", f"远低于同档二手价 ¥{used_median:.0f}（仅 {price/used_median*100:.0f}%）", None, None
    if base:
        one_hand_low = base[0] if len(base) > 0 else None
        # 二手不可能低于一手的 15%，那基本是配件/空盒/定金
        # 基准表里个别规格的下界可能缺失，缺了就跳过这条判定
        if one_hand_low is not None:
            try:
                one_hand_low = float(one_hand_low)
            except (TypeError, ValueError):
                one_hand_low = None
        if one_hand_low is not None and one_hand_low > 0 and price < one_hand_low * 0.15:
            return "reject", f"低于一手价下限 ¥{one_hand_low:.0f} 的 15%（疑似配件）", None, None
    return "ok", "", None, None


def explain_level(level):
    return {"ok": "正常", "review": "异常", "reject": "剔除"}.get(level, level)