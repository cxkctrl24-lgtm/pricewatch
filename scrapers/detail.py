# -*- coding: utf-8 -*-
"""商品详情页 SKU 解析：一个标题挂多个规格时，列出每个规格实际卖多少钱。

关键发现（抓详情页 HTML 实证）：
淘宝把 SKU 数据直接内嵌在页面 HTML 里 ——
  skuBase: {"skus": [{"propPath": "1627207:28320;...", "skuId": "6313179563805"}, ...],
            "props": [{ "name": "颜色分类", "values": [{"name": "影锋 黑色", "vid": ...}] }]}
  skuCore: {"sku2info": {"6313179563805": {"price": {"priceText": "4169", "priceMoney": "416900"}}}}

所以**不用点规格**，直接从 HTML 里抠这两个 JSON 就能拿到全部规格的完整价格表。
京东/拼多多同理，都有 window.pageConfig 或类似的内嵌对象。

注意：详情页比列表页重得多，只在用户主动点「看规格」时才访问，不做批量抓取。
"""
import json
import re

from ..db import log

# 从 HTML 里抠 JSON 对象：先定位 key，再按大括号配对扫出完整对象
JSON_BLOCK_TMPL = r'%s"\s*:\s*(\{)'

PRICE_TEXT_RE = re.compile(r"[¥￥]\s*([\d,]+(?:\.\d{1,2})?)")


def _extract_json_obj(html, key):
    """从 HTML 文本里找 "key":{...} 并返回解析后的 dict（大括号配对，正确处理字符串里的括号）。"""
    idx = html.find('"%s"' % key)
    if idx < 0:
        return None
    start = html.find("{", idx)
    if start < 0:
        return None

    depth = 0
    in_str = False
    esc = False
    i = start
    n = len(html)
    while i < n:
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    raw = html[start:i + 1]
                    try:
                        return json.loads(raw)
                    except Exception:
                        # 有些站点会塞 unicode 转义或单引号，尽力而为
                        try:
                            return json.loads(raw.replace("\\'", "'"))
                        except Exception:
                            return None
        i += 1
    return None


def _money(s):
    """'4169' / '416900'（分）/ '4169.05' → float 元"""
    if not s:
        return None
    try:
        v = float(str(s).replace(",", "").strip())
    except ValueError:
        return None
    if v <= 0:
        return None
    # 淘宝 priceMoney 是分（416900 → 4169.00），priceText 是元
    return v


def parse_taobao(html):
    """返回 (规格列表, [{skuId, 规格名, 价格}])"""
    base = _extract_json_obj(html, "skuBase") or {}
    core = _extract_json_obj(html, "skuCore") or {}

    props = base.get("props") or []
    # propPath 形如 "1627207:28320;20509:28314"，值用 props[].values[].vid 映射成名字
    vid2name = {}
    for p in props:
        pname = p.get("name") or ""
        for v in (p.get("values") or []):
            vid = str(v.get("vid") or v.get("valueId") or "")
            vname = v.get("name") or v.get("valueName") or ""
            if vid:
                vid2name[vid] = f"{pname}:{vname}" if pname else vname

    skus = base.get("skus") or []
    sku2info = (core.get("sku2info") or {})

    out = []
    for s in skus:
        sku_id = str(s.get("skuId") or "")
        path = s.get("propPath") or ""
        parts = []
        for seg in path.split(";"):
            vid = seg.split(":")[-1]
            parts.append(vid2name.get(vid, vid))
        spec = " / ".join([p for p in parts if p and p != "-1"]) or "默认规格"

        info = sku2info.get(sku_id) or {}
        price = info.get("price") or {}
        # priceText 优先（已是元），否则 priceMoney 按分换算
        ptext = price.get("priceText")
        pmoney = price.get("priceMoney")
        val = None
        if ptext:
            val = _money(ptext)
        elif pmoney:
            try:
                val = round(float(pmoney) / 100.0, 2)
            except (TypeError, ValueError):
                val = None
        if val is None:
            continue
        out.append({
            "sku_id": sku_id,
            "spec": spec,
            "price": val,
            "price_title": price.get("priceTitle") or "",
        })
    return props, out


def parse_jd(html):
    """京东：规格在 window.pageConfig 或 #choose-attrs，价格需要逐规格请求，
    这里先给出可读的规格列表 + 页面主价格。"""
    specs = []
    m = re.search(r'window\.pageConfig\s*=\s*(\{)', html)
    if m:
        obj = _extract_json_obj(html, "pageConfig")
        if obj:
            for k in ("skuColor", "skuSize", "skuType"):
                v = obj.get(k)
                if v:
                    specs.extend([x for x in str(v).split(",") if x.strip()])
    # 规格区文本兜底
    for mm in re.finditer(r'<a[^>]*class="[^"]*li[^"]*"[^>]*>([^<]{2,24})</a>', html):
        t = mm.group(1).strip()
        if t and t not in specs:
            specs.append(t)
    return specs[:20], []


def parse_pdd(html):
    """拼多多：goods.html 里有 window.rawData 或 __NEXT_DATA__，含 sku 数组。"""
    skus = []
    for key in ("rawData", "skuInfo", "goodsSku", "__NEXT_DATA__"):
        obj = _extract_json_obj(html, key)
        if not obj:
            continue
        # 在对象里递归找带 price/group_price 的 sku
        def walk(o, depth=0):
            if depth > 6:
                return
            if isinstance(o, dict):
                if (o.get("skuId") or o.get("sku_id")) and (
                    o.get("groupPrice") or o.get("price") or o.get("group_price")
                ):
                    p = o.get("groupPrice") or o.get("price") or o.get("group_price")
                    try:
                        val = float(p) / 100.0
                    except (TypeError, ValueError):
                        val = None
                    if val and val > 0:
                        name = o.get("spec") or o.get("specs") or o.get("skuName") or ""
                        if isinstance(name, list):
                            name = " / ".join(str(x) for x in name)
                        skus.append({
                            "sku_id": str(o.get("skuId") or o.get("sku_id")),
                            "spec": str(name)[:60] or "规格",
                            "price": round(val, 2),
                            "price_title": "",
                        })
                for v in o.values():
                    walk(v, depth + 1)
            elif isinstance(o, list):
                for v in o[:200]:
                    walk(v, depth + 1)
        walk(obj)
        if skus:
            break
    # 去重
    seen, uniq = set(), []
    for s in skus:
        if s["sku_id"] in seen:
            continue
        seen.add(s["sku_id"])
        uniq.append(s)
    return [], uniq[:30]


PARSERS = {"taobao": parse_taobao, "jd": parse_jd, "pdd": parse_pdd}


def fetch_sku_prices(session, platform, url, timeout_ms=None):
    """打开详情页并解析内嵌 SKU JSON。返回 (status, message, payload)。

    timeout_ms 传 None 时用 config.NAV_TIMEOUT_MS。批量补读详情页时
    会传更短的值（config.SKU_DETAIL_TIMEOUT_MS）—— 详情页读不到就算了，
    不该为了一件商品把整轮采集拖住。
    """
    page = session.page()
    to = int(timeout_ms or config.NAV_TIMEOUT_MS)
    log("info", f"[{platform}] 解析详情页规格: {url[:80]}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=to)
        # 等待固定量，不再用 2 秒硬等：按超时预算比例给，最少留 0.8 秒
        page.wait_for_timeout(min(2000, max(800, to // 5)))
        html = page.content()
    except Exception as e:
        log("warn", f"[{platform}] 详情页打开失败: {str(e)[:120]}")
        return "error", str(e)[:160], {}

    parser = PARSERS.get(platform)
    if not parser:
        return "error", "不支持的平台", {}

    try:
        props, skus = parser(html)
    except Exception as e:
        log("warn", f"[{platform}] SKU 解析异常: {str(e)[:120]}")
        return "error", f"解析异常: {str(e)[:100]}", {}

    title = ""
    m = re.search(r"<title>([^<]{2,120})</title>", html)
    if m:
        title = m.group(1).strip()

    if skus:
        skus.sort(key=lambda x: x["price"])
        msg = f"{len(skus)} 个规格，¥{skus[0]['price']:.0f} 起"
    else:
        # 没有 SKU 明细就退回页面第一个价格
        prices = [float(x.replace(",", "")) for x in PRICE_TEXT_RE.findall(html[:8000])]
        msg = "规格明细解析不到（可能单规格商品）"
        if prices:
            skus = [{"sku_id": "", "spec": "页面显示价", "price": prices[0], "price_title": ""}]
            msg = f"页面价 ¥{prices[0]:.0f}"

    return "ok", msg, {
        "title": title,
        "url": url,
        "specs": [v.get("name") for p in props for v in (p.get("values") or [])][:20] if props else [],
        "sku_prices": skus[:30],
        "sku_count": len(skus),
    }


def guess_platform(url):
    u = (url or "").lower()
    if "taobao" in u or "tmall" in u:
        return "taobao"
    if "jd.com" in u or "jd.hk" in u:
        return "jd"
    if "yangkeduo" in u or "pinduoduo" in u:
        return "pdd"
    return None