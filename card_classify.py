# -*- coding: utf-8 -*-
"""商品卡 vs 广告位/推荐位判别。

设计原则（有实测依据，见 analysis/llm_page_analysis.md）：
  · **规则为主，模型只兜边界**。
    实测规则版判广告位 11/12 正确、耗时 0 秒；
    全交给 9b 是 16/16 正确，但 0.35 秒/张 × 75 张/页 = 26 秒（慢 262 倍）。
    所以：规则能定的立刻定，只有规则拿不准的才问模型。
  · 规则拿不准的情形很少（预估 <15%），模型开销因此可控。

规则判据：
  1. 广告位特征词 + **没有具体规格数字**（DDRx + 容量）→ 广告
  2. 标题过短（<5 字）或完全没有商品词 → 广告
  3. 同时含多个栏目词（猜你喜欢/热销榜/领券…）→ 广告
  4. 有 DDRx + 容量 + 价格 → 商品（直接放行，不问模型）
"""
import json
import re
import urllib.request

from . import config
from .db import log

# 广告位/推荐位/导航的高频词
AD_WORDS = (
    "品牌补贴", "大牌好物", "每日精选", "点击查看", "猜你喜欢", "为你推荐", "更多好物",
    "百亿补贴", "官方立减", "限时秒杀", "全场包邮", "本店推荐", "进店逛逛", "关注店铺",
    "领券", "热销榜", "主会场", "领券中心", "立即抢购", "换一批", "浏览更多",
    "热门搜索", "查看更多", "去抢购", "相似商品", "为你精选", "搜索发现",
    "立即领取", "进入会场", "加入购物车", "分享有礼", "邀请好友",
)

# 明确是商品页面的信号：DDRx 代数 + 容量
SPEC_GEN_RE = re.compile(r"d[dD][rR]\s*[345Dd]", re.I)
SPEC_CAP_RE = re.compile(r"\d+\s*[Gg][Bb]?")
GPU_RE = re.compile(r"(rtx|gtx|rx)\s*\d{3,4}", re.I)
PRICE_RE = re.compile(r"[¥￥]\s*\d")

OLLAMA = getattr(config, "LLM_OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
MODEL = getattr(config, "LLM_CARD_MODEL", "qwen3.5:9b")
TIMEOUT_S = 15


def _has_spec(text):
    """有没有具体规格：内存要 DDRx+容量，显卡要型号。"""
    t = text or ""
    if GPU_RE.search(t):
        return True
    return bool(SPEC_GEN_RE.search(t)) and bool(SPEC_CAP_RE.search(t))


def what_ad_words(text):
    t = text or ""
    return [w for w in AD_WORDS if w in t]


def rule_judge(text):
    """纯规则判定。返回 "goods" / "ad" / None（拿不准，需要问模型）。

    0 耗时的这一层要尽量覆盖，把模型调用压到最低。
    """
    t = (text or "").strip()
    if not t:
        return "ad"
    if len(t) < 5:
        return "ad"

    hits = what_ad_words(t)
    has_spec = _has_spec(t)
    has_price = bool(PRICE_RE.search(t))

    # 有具体规格 + 有价格 → 明确是商品卡，直接放行（连模型都不问）
    if has_spec and has_price:
        return "goods"

    # 广告词命中 且 没有具体规格 → 广告
    if hits and not has_spec:
        return "ad"

    # 多个栏目词（≥2）且无价格 → 广告
    if len(hits) >= 2 and not has_price:
        return "ad"

    # 有价格但规格不全 → 拿不准，交给模型
    if has_price and not has_spec:
        return None

    # 没价格 → 基本上不是商品卡（商品卡的判据必须有价）
    if not has_price:
        return "ad"

    return None


def _call_model(text):
    """问模型是不是商品卡。返回 True / False / None（失败）。"""
    prompt = (
        "/no_think\n"
        "这是一段电商页面的文字。判断它是「一个商品的卡片」还是「页面导航/广告/推荐位」。\n\n"
        "是商品卡 = 有【商品名称】+【一个价格数字】。哪怕商品名很简略（如「精英全固 DDR4 8G」）也算。\n"
        "不是 = 没有任何商品名，或只有栏目名/促销口号/搜索词/店铺入口。\n\n"
        f"文本：{text}\n\n"
        "只回答 YES 或 NO："
    )
    body = {"model": MODEL, "prompt": prompt, "stream": False, "think": False,
            "options": {"temperature": 0, "num_predict": 8}}
    req = urllib.request.Request(
        OLLAMA, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        out = (d.get("response") or "").strip().upper()
        if "YES" in out:
            return True
        if "NO" in out:
            return False
        return None
    except Exception:
        return None


def is_goods_card(text, use_model=None):
    """判断一条页面文本是不是商品卡。返回 (bool, 来源)。

    来源标记：'rule'（规则判定）/ 'model'（模型判定）/ 'fallback'（模型不可用时的保守选择）
    """
    verdict = rule_judge(text)
    if verdict == "goods":
        return True, "rule"
    if verdict == "ad":
        return False, "rule"

    # 拿不准 → 问模型（仅这一小部分会走网络）
    if use_model is None:
        use_model = getattr(config, "LLM_CARD_CLASSIFY", True)
    if not use_model:
        # 不启用模型时保守选择：有价就算商品（宁可多收，后面还有假货过滤）
        return bool(PRICE_RE.search(text or "")), "fallback"

    r = _call_model(text)
    if r is None:
        return bool(PRICE_RE.search(text or "")), "fallback"
    return r, "model"


def filter_cards(cards, use_model=None, max_model_calls=None):
    """批量过滤：返回 (保留的卡片列表, 统计)。

    统计里能看清规则覆盖了多少、模型被调了多少 —— 用来验证成本可控。
    """
    kept = []
    stats = {"total": len(cards), "rule": 0, "model": 0, "fallback": 0, "dropped": 0}
    model_budget = max_model_calls if max_model_calls is not None else 999

    for c in cards:
        text = c.get("cardText") or c.get("title") or ""
        # 预算用完后不再问模型，退化成保守规则
        um = use_model
        if stats["model"] >= model_budget:
            um = False
        ok, src = is_goods_card(text, use_model=um)
        stats[src] = stats.get(src, 0) + 1
        if ok:
            kept.append(c)
        else:
            stats["dropped"] += 1

    return kept, stats
