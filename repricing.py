# -*- coding: utf-8 -*-
"""详情页 SKU 定价：把「一件商品」拆成「多个规格分类的真实价格」。

为什么需要它（实测案例）：
一个标题挂 15 个 SKU、横跨 DDR3/DDR4 与 4G~32G 的商品，
列表页只显示最低那个价。拿列表价 + 标题分类，必然出错：

  标题：七彩虹DDR4台式机电脑8G16G2666内存条3200 全新正品
  列表价：¥68
  实际 SKU：
    4G DDR3 1600【1.35v】   ¥69    ← ¥68 就是这一档，被算成了「DDR4 8G 最低价」
    8G DDR3 1600【1.35v】   ¥115
    8GB DDR4 2666 C19       ¥339
    16GB DDR4 3200 C22      ¥699
    32G(16G×2) DDR4 3200    ¥1339

本模块把每个 SKU **自己**归类（SKU 名自带代数+容量，是唯一干净的输入），
让一件商品向多个分类各贡献一个真实价格。这样：
  · 「DDR4 8G」拿到的是 ¥339，不再是 DDR3 的 ¥69
  · 「DDR3 8G」也能正确收录 ¥115（如果关心旧代的话）

设计要点：
1. SKU 名是主体。标题只在 SKU 名缺维度时补位，且代数补位要求
   父标题只含一个代数 —— 多代标题不许猜。
2. 占位类 SKU（「批量购可咨询客服」）不产出分类。
3. 唯一纯逻辑、不依赖浏览器 —— 抓取在 scrapers/detail.py，这里只做归类。
"""
import re

from .classify import classify, category_key

# 无规格含义的 SKU 名：占位/引导/客服/运费/赠品
NOISE_RE = re.compile(
    r"客服|咨询|批量|运费|补差|差价|赠品|备注|下单|联系|色卡|选项|不发货|勿拍|测试|套餐[一二三]",
    re.I,
)

# 值不值得进详情页：标题挂多个容量或跨代
CAP_RE = re.compile(r"(\d{1,3})\s*[Gg][Bb]?")
CAPS_VALID = {2, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128}
GEN_PATS = (("DDR5", r"DDR\s*5"), ("DDR4", r"DDR\s*4"),
            ("DDR3", r"DDR\s*3"), ("DDR2", r"DDR\s*2"))


def _gens(title):
    """标题里出现的所有内存代数。"""
    return sorted({g for g, p in GEN_PATS if re.search(p, title or "", re.I)})


def _caps(title):
    """标题里出现的所有内存容量（去重升序）。"""
    return sorted({int(x) for x in CAP_RE.findall(title or "") if int(x) in CAPS_VALID})


def needs_detail(title):
    """这件商品的列表价能不能直接代表某一档？不能就值得进详情页。

    多容量或多代 → 列表页那个低价只对应其中一档，必须拆。
    """
    return len(_caps(title)) >= 2 or len(_gens(title)) >= 2


def sku_spec_key(parent_title, sku_name):
    """把一个 SKU 名合成 classify() 的输入，返回 (分类键, 分类字典, 说明)。

    **只用 SKU 名做主体**。父标题只在 SKU 名缺维度时补位，
    代数补位有严格条件（父标题必须是单一代数）——
    否则「DDR4 8G16G」这种多代标题会把 DDR3 的 SKU 也补成 DDR4，
    这正是错价的来源。
    """
    name = (sku_name or "").strip()
    parent = (parent_title or "").strip()

    c = classify(name, None)
    notes = []

    # 代数：SKU 名没写才从父标题继承；父标题多代则不猜
    if not c.get("gen"):
        pg = _gens(parent)
        if len(pg) == 1:
            c["gen"] = pg[0]
            notes.append(f"代={pg[0]}(继承)")
        else:
            notes.append("代数无法确定")
            return None, c, "；".join(notes)

    # 容量：SKU 名没写就没法归类（不拿父标题的容量硬套）
    if not c.get("cap"):
        notes.append("容量缺失")
        return None, c, "；".join(notes)

    # 台式/笔记本：SKU 名一般不带，从父标题继承（不参与代数判定，安全）
    if c.get("mem_type") == "通用":
        pt = classify(parent, None).get("mem_type") or "通用"
        if pt != "通用":
            c["mem_type"] = pt
            notes.append(f"机型={pt}(继承)")

    # 品牌从父标题继承（只影响展示标签，不影响比价档位）
    if not c.get("brand"):
        b = classify(parent, None).get("brand")
        if b:
            c["brand"] = b

    key = category_key({"spec": c}, None)
    return key, c, "；".join(notes)


def reprice_from_skus(parent_title, skus):
    """核心出口：把 SKU 列表拆成 {规格分类: 该分类的真实最低价}。

    返回 (by_cat, skipped)：
      by_cat  {分类键: {price, sku, sku_id, gen, cap, count, freq, mem_type, brand, note, others}}
      skipped [{spec, price, why}]   —— 占位 SKU 或无法归类的 SKU
    """
    by_cat = {}
    skipped = []

    for s in skus:
        spec = s.get("spec") or ""
        price = s.get("price")
        if not price or price <= 0:
            continue
        if NOISE_RE.search(spec):
            skipped.append({"spec": spec, "price": price, "why": "占位/非商品 SKU"})
            continue
        key, c, why = sku_spec_key(parent_title, spec)
        if not key or key == "未分类":
            skipped.append({"spec": spec, "price": price, "why": why or "无法归类"})
            continue
        cur = by_cat.get(key)
        if cur is None or price < cur["price"]:
            by_cat[key] = {
                "price": round(float(price), 2),
                "sku": spec,
                "sku_id": s.get("sku_id") or "",
                "gen": c.get("gen"), "cap": c.get("cap"), "count": c.get("count"),
                "freq": c.get("freq"), "mem_type": c.get("mem_type"),
                "brand": c.get("brand"), "note": why,
            }
        # 同一分类下的其他 SKU 记成候选，方便回看"是不是取错了最便宜那档"
        by_cat[key].setdefault("others", []).append({"spec": spec, "price": price})

    return by_cat, skipped


def pick_for_group(by_cat, group_id):
    """只保留属于当前关键词组的分类，并按价格升序。

    group_id 是 ddr4/ddr5/rtx50；用它过滤掉不相干的档位
    （搜 DDR4 时，这件商品里 DDR3 的档位不该进 DDR4 的统计）。
    """
    want = {"ddr4": "DDR4", "ddr5": "DDR5", "rtx50": "RTX"}.get(group_id or "")
    out = []
    for key, info in by_cat.items():
        if want and want not in (info.get("gen") or "") and want not in key:
            continue
        out.append((key, info))
    out.sort(key=lambda kv: kv[1]["price"])
    return out


def best_for_group(by_cat, group_id):
    """这件商品在**当前组**里的真实最低档：返回 (分类键, 信息) 或 (None, None)。

    搜 DDR4 时，一件挂着 DDR3 和 DDR4 的商品，这里拿到的会是 DDR4 那一档，
    而不是列表页那个属于 DDR3 的 ¥69。
    """
    picked = pick_for_group(by_cat, group_id)
    if not picked:
        return None, None
    return picked[0]
