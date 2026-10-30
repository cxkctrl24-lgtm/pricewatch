# -*- coding: utf-8 -*-
"""假商品过滤器。

真实电商里有一批"钓鱼型"商品：标题把所有规格都堆一遍，低到离谱的价格吸引点击，
点进去发现要么是配件、要么是二手拆机、要么根本不是那个东西。典型特征：

  · 价格远低于同规格市场价（¥365 的 DDR5 16G，正常价位 ¥600+）
  · 标题关键词堆砌：同时写"DDR4"和"DDR5"、"台式"和"笔记本"、"8G/16G/32G/64G"全列一遍
  · 店铺是通用名：正品内存硬盘经销商 / 中国内存供应商 / 电码堡 / tb12345678的小店
  · 标题里没有任何品牌名
  · 标题里带"配件/支架/散热片/贴纸/空盒/模型/假"等非商品词

打分制：命中若干项就判定可疑，并给出理由。宁可少量误杀，也别让假价污染最低价。
"""
import re

# ---------------- 1. 非商品词（真商品不会长这样） ----------------
FAKE_WORDS = (
    "配件", "支架", "散热片", "贴纸", "空盒", "包装盒", "模型", "摆件",
    "展示", "样品", "海报", "说明书", "光盘", "转接卡", "延长线",
    "代拍", "代购服务", "维修服务", "回收", "换新服务", "定金", "订金",
    "预约", "补差价", "差价链接", "拍错", "测试链接", "勿拍", "非实物",
    "议价", "询价", "来图定制", "定制款", "看货", "咨询客服",
)

# ---------------- 2. 通用店名（这类店名往往不是正经商家） ----------------
GENERIC_SHOP_PATTERNS = (
    r"^正品.*(经销|专营|批发)",
    r"^中国.*(供应商|货源|批发)",
    r"^(数码|电子|电脑)(城|商城|广场)",
    r"^(IT|it).*商城",
    r"^tb\d+",                 # 淘宝个人店，纯 tb+数字
    r"tb\d+的小店",
    r"^\d+号店",
    r"^(好物|优品|精选|精品)(推荐|优选|严选)?$",
    r"^(厂家|工厂)(直销|直供|清仓)",
    r"^.*(批发|批发店|批发商)$",
    r"^(实惠|便宜|特价|清仓|甩卖).*",
)

# ---------------- 3. 品牌库（和 classify.py 同源，标题一个品牌都没有就很可疑） ----------------
BRAND_HINTS = (
    "金士顿", "kingston", "海盗船", "corsair", "威刚", "adata", "金泰克", "tigo",
    "七彩虹", "铭瑄", "maxsun", "三星", "samsung", "英睿达", "crucial", "美光", "micron",
    "海力士", "hynix", "光威", "gloway", "阿斯加特", "asgard", "芝奇", "g.skill",
    "宏想", "枭鲸", "王储", "汉存", "储奇", "玖合", "宇瞻", "apacer", "十铨", "team",
    "影驰", "galax", "科赋", "klew", "朗科", "netac", "金百达", "kingbank", "致钛",
    "雷克沙", "lexar", "pny", "惠普", "hp", "联想", "lenovo", "戴尔", "dell",
    "华硕", "asus", "微星", "msi", "技嘉", "gigabyte", "耕升", "gainward", "万丽",
    "映众", "inno3d", "索泰", "zotac", "蓝宝石", "sapphire", "迪兰", "瀚铠", "撼讯",
    "佰维", "biwin", "长鑫", "cxmt", "金百达", "朗科", "威刚", "创见", "transcend",
    "紫光", "unils", "光威", "gloway", "鑫联", "铭鑫",
)

# 二手 / 拆机 / 翻新 —— 不是假货，但也不能当"全新"比价
USED_WORDS = ("二手", "拆机", "拆解", "翻新", "矿卡", "维修过", "已用过", "回收", "散片")


def _norm(t):
    return (t or "").lower()


def check_title_padding(title, group_id=None):
    """标题堆砌检测：同时涵盖多个互斥规格 = 钓鱼标题。

    group_id 用来判断「跨代」是否算混淆：
      · 搜 DDR4 时标题出现 DDR3/DDR2 → 便宜的那一档几乎都是旧代，必须剔除
      · 搜 DDR5 时出现 DDR4 同理
    跨代比单纯的多容量更危险：多容量至少还是同代（只是不知道对应哪档，可去
    详情页读 SKU），跨代则是拿旧代的价格冒充当代的最低价。
    """
    low = _norm(title)
    reasons = []

    has_ddr5 = ("ddr5" in low) or ("ddr 5" in low)
    has_ddr4 = ("ddr4" in low) or ("ddr 4" in low)
    has_ddr3 = ("ddr3" in low) or ("ddr 3" in low)
    has_ddr2 = ("ddr2" in low) or ("ddr 2" in low)

    gens = set()
    for name, flag in (("DDR5", has_ddr5), ("DDR4", has_ddr4),
                       ("DDR3", has_ddr3), ("DDR2", has_ddr2)):
        if flag:
            gens.add(name)

    if len(gens) >= 2:
        reasons.append("标题跨代堆砌（" + "/".join(sorted(gens)) + "）")

    # 搜当代时混进旧代 —— 直接判为混淆，因为低价对应的一定是旧代那个 SKU
    expect = {"ddr5": "DDR5", "ddr4": "DDR4"}.get(group_id or "")
    old_gens = {"DDR3", "DDR2"}
    if expect and expect not in ("DDR3", "DDR2"):
        sneaked = gens & old_gens
        if sneaked:
            reasons.append(
                "搜 " + expect + " 却混入旧代（" + "/".join(sorted(sneaked)) + "），最低价多半是旧代 SKU")

    has_desk = ("台式" in low) or ("台式机" in low) or ("dimm" in low and "sodimm" not in low)
    has_lap = ("笔记本" in low) or ("笔电" in low) or ("sodimm" in low) or ("pc5-" in low)
    if has_desk and has_lap:
        reasons.append("标题同时写台式机和笔记本")

    # 容量堆砌：一次列 3 个以上不同容量
    caps = set()
    for m in re.finditer(r"(\d{1,3})\s*[gG](?:[Bb])?", title or ""):
        v = int(m.group(1))
        if v in (4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128):
            caps.add(v)
    if len(caps) >= 3:
        reasons.append(f"标题堆了 {len(caps)} 种容量（{'/'.join(str(c) for c in sorted(caps))}）")

    # 频率堆砌
    freqs = set()
    for m in re.finditer(r"(\d{4})\s*(?:MHz|MHZ|mhz|频率)?", title or ""):
        v = int(m.group(1))
        if 2000 <= v <= 9000:
            freqs.add(v)
    if len(freqs) >= 4:
        reasons.append(f"标题堆了 {len(freqs)} 种频率")

    # 型号堆砌（显卡）
    gpu_models = set(re.findall(r"(50[6789]0)", title or ""))
    if len(gpu_models) >= 3:
        reasons.append(f"标题堆了 {len(gpu_models)} 个显卡型号")

    return reasons


def check_fake_words(title):
    low = _norm(title)
    hits = [w for w in FAKE_WORDS if w in low]
    return hits


def check_shop(shop):
    """店名可疑度。返回命中的模式。"""
    s = (shop or "").strip()
    if not s:
        return ["没有店铺名"]
    for pat in GENERIC_SHOP_PATTERNS:
        if re.search(pat, s, re.I):
            return [f"店名通用（{s}）"]
    return []


def check_brand(title):
    low = _norm(title)
    return any(b in low for b in BRAND_HINTS)


def check_price_anomaly(price, peers):
    """和同分类的其他商品比价。peers 是同分类所有价格列表。
    < 中位数的 45% 就判定异常（真实低价通常是二手/拆机，那种会被 used 标记）。"""
    if not peers or len(peers) < 3:
        return None
    import statistics
    med = statistics.median(peers)
    if med <= 0:
        return None
    ratio = price / med
    if ratio < 0.45:
        return f"价格仅同类中位数的 {ratio*100:.0f}%（同类中位 ¥{med:.0f}）"
    return None


def score(item, peers=None, group_id=None):
    """给一件商品打分。返回 (是否可疑, 分数, 理由列表, 是否二手)。"""
    title = item.get("title") or ""
    shop = item.get("shop") or ""
    price = item.get("price") or 0

    reasons = []
    points = 0

    has_brand = check_brand(title)

    # 标题堆砌：只有"没有品牌"时才当强信号。
    # 因为正规商家挂多规格（8G/16G/32G 都是选项）是淘宝的常规做法，
    # 有品牌的多规格标题不该误杀。
    pad = check_title_padding(title, group_id=group_id)
    if pad:
        reasons.extend(pad)
        # 例外：跨代混淆（搜 DDR4/DDR5 却混进 DDR3/DDR2）即使有品牌也必须剔除 ——
        # 这种标题打出 ¥69 的最低价，那个价一定属于旧代那一档 SKU，
        # 拿它当"当前代的最低价"是彻头彻尾的错误数据。
        cross_gen = any("混入旧代" in r or "跨代堆砌" in r for r in pad)
        if cross_gen:
            points += 5
        elif not has_brand:
            points += len(pad) * 2
        else:
            points += 0         # 有品牌的多规格标题：正常，不扣分

    # 非商品词：配件/差价链接/议价 这种是硬信号，有没有品牌都要扣
    fake = check_fake_words(title)
    if fake:
        reasons.append(f"标题含非商品词：{'/'.join(fake[:3])}")
        points += 5

    # 店名通用：有品牌的话降权（有些正规小代理店名也一般）
    shop_hit = check_shop(shop)
    if shop_hit:
        reasons.extend(shop_hit)
        points += 1 if has_brand else 2

    if not has_brand:
        reasons.append("标题没有任何品牌名")
        points += 1

    # 价格异常：这才是最硬的指纹 —— 远低于同类中位数
    pa = check_price_anomaly(price, peers)
    if pa:
        reasons.append(pa)
        points += 3 if not has_brand else 2

    is_used = any(w in _norm(title) for w in USED_WORDS)
    if is_used:
        reasons.append("二手/拆机/翻新")

    # 判定门槛：4 分以上算可疑
    return points >= 4, points, reasons, is_used