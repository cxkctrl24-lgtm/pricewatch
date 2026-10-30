# -*- coding: utf-8 -*-
"""全局配置：想改关键词/价格区间/采集深度，只改这里。"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
PROFILE_DIR = os.path.join(DATA_DIR, "profiles")   # 每个平台的浏览器登录态都在这里
DB_PATH = os.path.join(DATA_DIR, "pricewatch.db")
CONFIG_JSON = os.path.join(DATA_DIR, "settings.json")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(PROFILE_DIR, exist_ok=True)

HOST = "127.0.0.1"
PORT = 8765

# 采集时是否静默运行（登录窗口永远可见）。调试时可用 PW_HEADLESS=1 关掉弹窗。
HEADLESS = os.environ.get("PW_HEADLESS", "0") == "1"

# ============ 搜索的词 ============
# price 是"看起来像这个配件的价格"区间，用来过滤掉不相关商品（比如 DDR5 搜到的 DDR4、机箱配件）
KEYWORD_GROUPS = [
    {
        "id": "ddr5",
        "name": "DDR5 内存",
        "keywords": ["DDR5 内存条 16G", "DDR5 内存条 32G", "DDR5 6000 16G*2"],
        "price": (120, 6000),
    },
    {
        "id": "ddr4",
        "name": "DDR4 内存",
        "keywords": ["DDR4 内存条 8G", "DDR4 内存条 16G", "DDR4 3200 16G*2"],
        "price": (40, 3000),
    },
    {
        "id": "rtx50",
        "name": "RTX 50 系显卡",
        "keywords": ["RTX 5060", "RTX 5070", "RTX 5070Ti", "RTX 5080", "RTX 5090"],
        "price": (1200, 60000),
    },
]

# ============ 平台 ============
PLATFORMS = {
    "taobao": {
        "home_url": "https://www.taobao.com/",
        "name": "淘宝",
        "login_url": "https://login.taobao.com/member/login.jhtml",
        "login_mode": "qrcode",
        "login_hint": "手机淘宝扫窗口里的二维码",
        "check_url": "https://i.taobao.com/my_taobao.htm",
        "search_url": "https://s.taobao.com/search?q={q}",
        "item_url_match": ("item.taobao.com", "detail.tmall.com", "chaoshi.detail.tmall.com"),
        "mobile": False,
        # 每个选择器都是"候选列表"，按顺序试，第一个出结果的用
        "selectors": {
            "item": [
                "div[class*='Card']",
                "div[class*='doubleCard']",
                "div[class*='item']",
                ".item.J_MouserOnverReq",
                "div[class*='itemWrapper']",
            ],
            "title": ["div[class*='title'] a", "a[class*='title']", "a[class*='Title']"],
            "price": ["div[class*='priceInt']", "div[class*='price']", "span[class*='price']", "div[class*='Price']"],
            "shop": ["div[class*='shopName']", "span[class*='shop']", "a[class*='shop']"],
        },
    },
    "jd": {
        "home_url": "https://www.jd.com/",
        "name": "京东",
        "login_url": "https://passport.jd.com/new/login.aspx",
        "login_mode": "qrcode",
        "login_hint": "京东 App 扫码，或用账号密码",
        "check_url": "https://home.jd.com/",
        "search_url": "https://search.jd.com/Search?keyword={q}&enc=utf-8",
        "item_url_match": ("item.jd.com", "item.m.jd.com"),
        "mobile": False,
        "selectors": {
            "item": ["li.gl-item", "div.gl-i-wrap", "li[class*='gl-item']"],
            "title": ["div.p-name a em", "div.p-name a", "a[href*='item.jd.com']"],
            "price": ["div.p-price i", "div.p-price strong", "i[class*='price']", "em[class*='price']"],
            "shop": ["div.p-shop a", "div.p-shop span", "a[class*='shop']"],
        },
    },
    "pdd": {
        "home_url": "https://mobile.yangkeduo.com/",
        "name": "拼多多",
        # 拼多多网页端只有手机号+验证码，没有扫码；用桌面版页面更好操作
        "login_url": "https://mobile.yangkeduo.com/login.html",
        "login_hint": "拼多多网页版没有扫码 —— 只能填手机号收验证码",
        "login_mode": "sms",
        "check_url": "https://mobile.yangkeduo.com/personal.html",
        "search_url": "https://mobile.yangkeduo.com/search_result.html?search_key={q}",
        "item_url_match": ("mobile.yangkeduo.com/goods.html", "yangkeduo.com/goods"),
        "mobile": True,
        "selectors": {
            "item": [
                "div[class*='goods']",
                "a[href*='goods.html']",
                "div[class*='item']",
            ],
            "title": ["div[class*='title']", "div[class*='name']", "span[class*='title']"],
            "price": ["div[class*='price']", "span[class*='price']", "div[class*='Price']"],
            "shop": ["div[class*='mall']", "span[class*='shop']", "div[class*='store']"],
        },
    },
}

# 采集深度
SCROLL_TIMES = 12          # 结果页往下滚多少次（懒加载更多商品）
SCROLL_REST_EVERY = 3      # 每滚这么多次，多停一下（模拟看一会儿再继续）

# ---------- 温和模式：把节奏压到最像真人的程度 ----------
GENTLE = True                    # 总开关，关掉就是原来的快跑模式
GENTLE_HOME_PAUSE = (2000, 3600)   # 进站后先在首页待这么久再搜索（毫秒）
GENTLE_KW_GAP = (3.0, 7.0)         # 两个关键词之间（秒）
GENTLE_PLATFORM_GAP = (6.0, 13.0)  # 两个平台之间（秒）
GENTLE_SCROLL_PAUSE = (1.0, 2.6)   # 每次滚动之间（秒）
GENTLE_REST_LONG = (3.0, 6.0)      # 滚动中途的长停顿（秒）

# ---------- 分平台节奏：谁更敏感就压更慢 ----------
# 京东对搜索频次最敏感（"访问频繁"来得又快又久），单独给它更慢的节奏。
PLATFORM_PACING = {
    "jd": {
        "scroll_times": 8,                    # 滚得少一点，减少请求次数
        "kw_gap": (10.0, 22.0),               # 关键词之间拉长
        "scroll_pause": (2.0, 4.5),
        "rest_long": (5.0, 10.0),
        "home_pause": (4000, 7000),           # 首页预热更久
        "cooldown": 1800,                     # 限流后冷却 30 分钟
    },
    "taobao": {
        "scroll_times": 12,
        "kw_gap": (4.0, 9.0),
        "scroll_pause": (1.0, 2.6),
        "rest_long": (3.0, 6.0),
        "home_pause": (2000, 3600),
        "cooldown": 900,
    },
    "pdd": {
        "scroll_times": 10,
        "kw_gap": (5.0, 10.0),
        "scroll_pause": (1.2, 3.0),
        "rest_long": (3.0, 7.0),
        "home_pause": (2500, 4200),
        "cooldown": 900,
    },
}


# 被平台限流（"访问频繁"）后冷却多久再继续。京东对这个特别敏感。
RATE_LIMIT_COOLDOWN = 600      # 秒，默认 10 分钟；按平台见 PLATFORM_PACING
MAX_ITEMS_PER_KEYWORD = 200
PAGE_TIMEOUT_MS = 45000
NAV_TIMEOUT_MS = 35000

# ---------- 详情页 SKU 校正 ----------
# 一件商品挂多个 SKU（横跨 DDR3/DDR4、4G~32G）时，列表页只暴露最低那个价。
# 拿它配标题分类会把「4G DDR3 ¥69」算成「DDR4 8G 最低价」（实测踩过）。
#
# 淘宝详情页在批量访问下会挂起（page.goto 超时都不按设定返回，实测卡 430 秒），
# 所以上一版默认关闭。现在配了 sku_cache（同一件商品 45 分钟内只读一次详情页），
# 二次访问直接命中缓存、完全不开页面，反复触发风控的问题被消掉 —— 重新开启。
# 若你的网络环境下详情页仍然拖慢采集，把它改回 False 即可。
SKU_REPRICE = True
# 每次采集最多补读几件详情页（缓存命中不计入这个数）
SKU_REPRICE_MAX_PER_KEYWORD = 8
# 每个关键词的校正**总时间预算**（秒）。超了就直接收手用列表价。
SKU_REPRICE_BUDGET = 90
# 单条详情页的超时（毫秒）
SKU_DETAIL_TIMEOUT_MS = 15000
# 详情页之间的间隔（秒）
SKU_REPRICE_GAP = (2.0, 3.5)

# ---------- 大模型解析兜底 ----------
# 仅在**正则一条都没抽到**时才调用本机 Ollama，让模型尽力补抽。
# 实测结论（analysis/llm_page_analysis.md）：本地小模型不能当主力 ——
# qwen3.5:9b 抽 40 条要 25 秒还漏 20%，而正则 0.1 秒抽 75 条全对。
# 所以只在"那一轮本来就要失败"时用它，正常采集零开销。
LLM_FALLBACK = True
LLM_OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
# 用 9b 不用 2b：实测 2b 在 40 条时崩溃且价格全填 0
LLM_MODEL = "qwen3.5:9b"


# 节奏自适应：被限流过就自动放慢，采顺了再逐步恢复。
# hits 来自 cooldown.json 的连续限流次数（1 次 → ×1.5，2 次 → ×2.5，3 次 → ×4）。
# 只有采成一次（cooldown.clear）才会清零 hits，所以放慢是"记住教训"的。
PACING_BOOST = {
    0: 1.0,      # 没被限过，正常速度
    1: 1.5,
    2: 2.5,
    3: 4.0,
}


def _boost_for(hits):
    """按连续限流次数取放慢系数（超过 3 次按 4 倍封顶）。"""
    try:
        h = int(hits or 0)
    except Exception:
        h = 0
    if h <= 0:
        return 1.0
    return PACING_BOOST.get(h, PACING_BOOST[max(PACING_BOOST)])


def _scale_range(rng, k):
    """把一个 (最小, 最大) 区间按系数 k 放大。"""
    try:
        lo, hi = rng
    except Exception:
        return rng
    return (float(lo) * k, float(hi) * k)


def pacing(platform):
    """取该平台的节奏参数，缺项回落到全局温和模式默认值。

    **会被限流历史自动放慢**：读取 cooldown.json 里该平台的连续被限次数，
    按 PACING_BOOST 放大所有等待区间（关键词间隔、滚动停顿、首页预热、
    平台间歇、冷却时长）。采成一次 cooldown.clear 后自动回到正常速度。
    """
    base = {
        "scroll_times": SCROLL_TIMES,
        "kw_gap": GENTLE_KW_GAP,
        "scroll_pause": GENTLE_SCROLL_PAUSE,
        "rest_long": GENTLE_REST_LONG,
        "home_pause": GENTLE_HOME_PAUSE,
        "cooldown": RATE_LIMIT_COOLDOWN,
        "boost": 1.0,
    }
    base.update(PLATFORM_PACING.get(platform, {}))

    # 读取限流历史（延迟导入，避免 config ←→ cooldown 循环依赖）
    try:
        from . import cooldown
        k = _boost_for(cooldown.hits(platform))
    except Exception:
        k = 1.0
    if k > 1.0:
        base["boost"] = k
        for key in ("kw_gap", "scroll_pause", "rest_long", "home_pause"):
            if base.get(key):
                base[key] = _scale_range(base[key], k)
        base["scroll_times"] = max(3, int(base.get("scroll_times", SCROLL_TIMES) / min(k, 2.0)))
        base["cooldown"] = float(base.get("cooldown") or RATE_LIMIT_COOLDOWN) * k
    return base

# 每日自动采集（本机时区）
SCHEDULE_ENABLED = True
SCHEDULE_TIME = "21:40"    # HH:MM，晚上跑避开平台日间的风控巡检高峰
SCHEDULE_PLATFORMS = ["taobao", "jd", "pdd"]

# 桌面窗口尺寸
DESKTOP_VIEWPORT = {"width": 1440, "height": 900}
MOBILE_VIEWPORT = {"width": 420, "height": 900}

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")