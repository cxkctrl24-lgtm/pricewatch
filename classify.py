# -*- coding: utf-8 -*-
"""商品分类器 v2：把「一个标题挂多个规格」的商品拆到真正可比的档位上。

v1 的问题（实测 318 件真实数据）：
  DDR5 16G 台式   38 件  ¥645 ~ ¥4799   ← 跨度 7 倍，一手二手混在一起，没法比价
  RTX 5060        32 件  ¥1650 ~ ¥9199   ← 跨度 5.6 倍
  DDR5 32G 套装 和 DDR5 32G 套装 台式 被拆成两类  ← 信息缺失导致分裂

v2 的改造：
  1. **容量维度**：把「单条容量」和「套装总容量」分开算，8G/16G/32G/64G 各自独立
     套装 16G*2 归到「16G×2」，不再和单条 32G 混为一谈
  2. **一手/二手**：condition 字段（new / used / refurb），价格分开统计
     二手单独一档，不污染一手最低价
  3. **频率档**：把频率归一成档位（4800/5200/5600/6000/6400/6800+），
     避免 6000 和 4800 混在一起比
  4. **缺失兜底**：台式/笔记本不知道时，不再分裂成"XX 16G"和"XX 16G 台式"两类，
     统一标为"通用"并合并统计
"""
import re

# ---------------- 容量 ----------------
# 套装：16G*2 / 8Gx2 / 16G×2 / 32G(16Gx2) / 16G*2条
CAP_SET_RE = re.compile(
    r"(\d{1,3})\s*[Gg]\s*(?:[Bb])?\s*(?:[*×xX]\s*(\d{1,2})|[条套枚根]\s*(\d{1,2}))"
)
CAP_RE = re.compile(r"(\d{1,3})\s*[Gg][Bb]?")

# 显存（显卡专用，避免和内存容量混淆）
VRAM_RE = re.compile(r"(\d{1,2})\s*[Gg][Bb]?\s*(?:显存|GDDR|gddr)")

# ---------------- 频率 ----------------
FREQ_RE = re.compile(r"(\d{4})\s*(?:MHZ|MHz|mhz|频率|Hz)?")

# ---------------- 类型 ----------------
DESKTOP_MARKS = ("台式", "台式机", "udimm", "台机", "dimm内存", "内存条台式", "pc内存")
LAPTOP_MARKS = ("笔记本", "笔电", "sodimm", "so-dimm", "轻薄本", "pc5-", "pc4-", "笔记本内存")

# ---------------- 显卡型号 ----------------
GPU_RE = re.compile(r"(50[6789]0)\s*(Ti|TI|ti)?", re.I)

# ---------------- 品牌 ----------------
BRANDS = [
    "金士顿", "Kingston", "海盗船", "Corsair", "威刚", "ADATA", "金泰克", "tigo",
    "七彩虹", "铭瑄", "maxsun", "三星", "Samsung", "英睿达", "Crucial", "美光", "Micron",
    "海力士", "Hynix", "光威", "Gloway", "阿斯加特", "Asgard", "芝奇", "G.SKILL",
    "宏想", "枭鲸", "王储", "汉存", "储奇", "玖合", "宇瞻", "Apacer", "十铨", "Team",
    "影驰", "Galax", "科赋", "KLEVV", "朗科", "Netac", "金百达", "KingBank", "致钛",
    "雷克沙", "Lexar", "PNY", "惠普", "HP", "联想", "Lenovo", "戴尔", "Dell",
    "华硕", "ASUS", "微星", "MSI", "技嘉", "GIGABYTE", "耕升", "GAINWARD", "万丽",
    "映众", "Inno3D", "电竞叛客", "索泰", "ZOTAC", "蓝宝石", "Sapphire", "迪兰",
    "瀚铠", "撼讯", "PowerColor", "丽台", "Leadtek", "佰维", "BIWIN", "长鑫", "CXMT",
    "宏基", "Acer", "紫光", "创见", "Transcend", "鑫联", "铭鑫", "晶刚", "GROWMYTH",
]

GPU_SERIES = [
    "猛禽", "ROG", "TUF", "大师", "巨齿鲨", "天选", "雪豹", "战斧", "火神",
    "魔鹰", "猎鹰", "雪鹰", "超级雕", "风魔", "Gaming", "追风", "踏雪",
    "炫光", "金属大师", "星曜", "名人堂", "HOF", "超级冰龙", "曜夜", "映雪",
    "万图师", "VENTUS", "魔龙", "幻影师", "神龙", "超龙", "SUPRIM", "机甲暴风",
    "电竞之心", "终结者", "瑷珈", "iGame", "九段", "水神", "Neptune", "白魔龙",
    "潜龙", "猎鹰", "星夜", "天启", "刃",
]

# ---------------- 一手 / 二手 ----------------
USED_MARKS = (
    "二手", "拆机", "拆解", "翻新", "矿卡", "用过", "回收", "散片", "二手价",
    "9成新", "95新", "99新", "8成新", "成色", "库存件", "拆自", "退役",
)
NEW_MARKS = ("全新", "新品", "正品全新", "原封", "未拆", "未开封", "盒装全新")


def _capacities_all(title):
    """标题里出现的所有「像内存容量」的数字（去重、升序）。

    只认 4/6/8/12/16/24/32/48/64/96/128 这些常见档位，避开频率、型号数字。
    """
    caps = set()
    for x in CAP_RE.findall(title or ""):
        v = int(x)
        if v in (4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128):
            caps.add(v)
    return sorted(caps)


def _first_capacity(title):
    """返回 (单条容量, 条数, 总容量)。

    注意：多容量标题（"8G 16G 32G 全兼容"）没法从标题判断列表价对应哪一档 ——
    这种条目会被 multi_cap=True 标出来，由采集层去详情页用 SKU 定价，
    而不是拿列表页最低价硬塞进某一档。
    """
    m = CAP_SET_RE.search(title)
    if m:
        single = int(m.group(1))
        count = m.group(2) or m.group(3)
        count = int(count) if count else 1
        if count > 1:
            return single, count, single * count
        return single, 1, single
    caps = _capacities_all(title)
    if not caps:
        return None, None, None
    # 多容量标题取最小（和列表页展示的低价对应）
    return min(caps), 1, min(caps)


def _first_freq(title):
    for m in FREQ_RE.finditer(title):
        v = int(m.group(1))
        if 2000 <= v <= 9000:
            return v
    return None


def _freq_band(freq):
    """把频率归一成档位，避免 4800 和 6000 混在一起比价。"""
    if not freq:
        return None
    if freq < 5000:
        return "≤4800"
    if freq < 5600:
        return "5200"
    if freq < 6000:
        return "5600"
    if freq < 6400:
        return "6000"
    if freq < 7000:
        return "6400"
    if freq < 7600:
        return "7000"
    return "7600+"


def _mem_type(title):
    low = title.lower()
    has_lap = any(k.lower() in low for k in LAPTOP_MARKS)
    has_desk = any(k.lower() in low for k in DESKTOP_MARKS)
    if has_lap and not has_desk:
        return "笔记本"
    if has_desk and not has_lap:
        return "台式"
    if has_desk and has_lap:
        return "台式"          # 两者都写通常是"通用"，按台式算
    # 线索推断
    if any(k in low for k in ("sodimm", "so-dimm", "pc5-", "pc4-", "笔记本")):
        return "笔记本"
    if any(k in low for k in ("马甲", "灯条", "rgb", "散热片", "dimm")):
        return "台式"
    return "通用"              # v2 改动：不再返回"未知"造成分类分裂


def _gpu_model(title):
    """多型号标题取第一个（和列表页低价对应）。"""
    best = None
    for m in GPU_RE.finditer(title or ""):
        pos = m.start()
        if best is None or pos < best[0]:
            model = f"RTX {m.group(1)}"
            if m.group(2) or (title[m.end():m.end() + 2] or "").lower() == "ti":
                model += " Ti"
            best = (pos, model)
    return best[1] if best else None


def _brand(title):
    low = title.lower()
    hits = [(len(b), b) for b in BRANDS if b.lower() in low]
    if not hits:
        return None
    hits.sort(reverse=True)
    zh = [h for h in hits if not h[1].isascii()]
    return (zh[0] if zh else hits[0])[1]


def _gpu_series(title):
    low = title.lower()
    hits = [s for s in GPU_SERIES if s.lower() in low]
    return max(hits, key=len) if hits else None


def condition(title):
    """一手 / 二手 / 翻新。二手优先级高于全新 —— 标题同时写"全新二手对比"的按二手算（保守）。"""
    low = (title or "").lower()
    if any(w in low for w in USED_MARKS):
        return "used"
    return "new"


def classify(title, group_id=None):
    """解析标题，返回规格字典。"""
    title = title or ""
    out = {
        "gen": None, "cap": None, "count": None, "cap_total": None,
        "freq": None, "freq_band": None, "mem_type": None,
        "gpu": None, "vram": None, "gpu_series": None,
        "brand": None, "condition": condition(title),
    }

    if re.search(r"DDR5|ddr5|DDR 5", title):
        out["gen"] = "DDR5"
    elif re.search(r"DDR4|ddr4|DDR 4", title):
        out["gen"] = "DDR4"
    elif re.search(r"DDR3|ddr3|DDR 3", title):
        # 老一代内存。原先这里没有分支，DDR3 标题会掉进下面的 group_id 兜底，
        # 被硬判成 DDR4 —— ¥69 的 4G DDR3 就这样混进了 DDR4 8G 的最低价里。
        out["gen"] = "DDR3"
    elif re.search(r"DDR2|ddr2|DDR 2", title):
        out["gen"] = "DDR2"
    elif group_id == "ddr5":
        out["gen"] = "DDR5"
    elif group_id == "ddr4":
        out["gen"] = "DDR4"

    # 标题里同时出现两代（"DDR4 兼容 DDR3"、多规格挂一起）→ 记下来，
    # 这种条目不能拿列表页价格去代表任何一代，后面会被判定为不可定价。
    gens = set()
    for g, pat in (("DDR5", r"DDR\s*5"), ("DDR4", r"DDR\s*4"),
                   ("DDR3", r"DDR\s*3"), ("DDR2", r"DDR\s*2")):
        if re.search(pat, title, re.I):
            gens.add(g)
    out["gens_all"] = sorted(gens)

    out["brand"] = _brand(title)

    gpu = _gpu_model(title)
    if gpu:
        out["gpu"] = gpu
        out["gpu_series"] = _gpu_series(title)
        v = VRAM_RE.search(title)
        if v:
            out["vram"] = int(v.group(1))
        else:
            cap, cnt, total = _first_capacity(title)
            if cap in (6, 8, 12, 16, 20, 24, 32):
                out["vram"] = cap
        return out

    cap, cnt, total = _first_capacity(title)
    out["cap"] = cap
    out["count"] = cnt
    out["cap_total"] = total
    out["caps_all"] = _capacities_all(title)

    # 标题挂着多个容量 → 列表页价格只代表其中最便宜那一档，不能代表整个商品。
    # 这种条目必须由详情页 SKU 定价（runner 会用它替换列表价）。
    out["multi_cap"] = len(out["caps_all"]) >= 2
    # 标题挂多代内存（DDR3/DDR4 一起卖）同理，而且更危险 —— 便宜的是旧代。
    out["multi_gen"] = len(out.get("gens_all") or []) >= 2

    out["freq"] = _first_freq(title)
    out["freq_band"] = _freq_band(out["freq"])
    out["mem_type"] = _mem_type(title)
    return out


def category_key(item, group_id=None):
    """**主分类**（用于横向比价）。

    内存 → 代数 + 单条容量 [+ ×条数] + 台式/笔记本 + 频率档 + [二手]
    显卡 → 型号 + 显存档 + [二手]

    一手和二手**必须分开** —— 二手 8G DDR5 ¥300 和全新 ¥700 是两回事，
    混在一起最低价永远是二手，一手价就看不见了。
    """
    c = item.get("spec") or classify(item.get("title", ""), group_id)
    parts = []

    if c.get("gpu"):
        parts.append(c["gpu"])
        if c.get("vram"):
            parts.append(f'{c["vram"]}G')
        if c.get("condition") == "used":
            parts.append("二手")
        return " ".join(parts)

    if c.get("gen"):
        parts.append(c["gen"])

    # 容量：单条 + 条数（套装明确标 ×N）
    if c.get("cap"):
        if c.get("count") and c["count"] > 1:
            parts.append(f'{c["cap"]}G×{c["count"]}')
        else:
            parts.append(f'{c["cap"]}G')

    if c.get("mem_type") and c["mem_type"] != "通用":
        parts.append(c["mem_type"])

    if c.get("freq_band"):
        parts.append(c["freq_band"])

    if c.get("condition") == "used":
        parts.append("二手")

    if not (c.get("gen") or c.get("cap")):
        return "未分类"
    return " ".join(parts)


def detail_key(item, group_id=None):
    """**细分标签**（带品牌/频率原值/系列），显示在商品行上。"""
    c = item.get("spec") or classify(item.get("title", ""), group_id)
    parts = []
    if c.get("gpu"):
        parts.append(c["gpu"])
        if c.get("vram"):
            parts.append(f'{c["vram"]}G')
        if c.get("gpu_series"):
            parts.append(c["gpu_series"])
    else:
        if c.get("freq"):
            parts.append(str(c["freq"]))
        if c.get("count") and c["count"] > 1:
            parts.append(f'{c["count"]}条装')
    if c.get("brand"):
        parts.append(c["brand"])
    if c.get("condition") == "used":
        parts.append("二手")
    return " ".join(parts) if parts else "—"


def spec_label(item, group_id=None):
    """兼容 v1 调用名，等价于 detail_key。"""
    return detail_key(item, group_id)