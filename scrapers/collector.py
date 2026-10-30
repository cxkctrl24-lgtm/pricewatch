# -*- coding: utf-8 -*-
"""三个平台的页面解析。每个平台一个 collect()，返回 (状态, 消息, items)。

淘宝/京东/拼多多的卡片 class 都带每次发版就变的哈希后缀
（taobao: doubleCard--gO3Bz6bu / jd: gl-item），所以选择器一律用 [class*=] 前缀匹配。

关键坑：淘宝卡片里的第一个 <a> 是店铺广告跳转（click.simba.taobao.com），
真正的商品链接排在它后面。所以必须**按商品域名筛选链接**，不能拿第一个。
"""
import hashlib
import random
import re
import time

from .. import config
from ..db import log

PRICE_RE = re.compile(r"(\d[\d,]*\.?\d*)")

# 淘宝卡片里的价格被拆成整数段 + 小数段
TAOBAO_FRAC_RE = re.compile(r'priceFloat[^"]*"[^>]*>(\d+)<')


def _rand(a, b):
    """随机毫秒等待，模拟真人节奏。"""
    return random.randint(int(a), int(b))


def parse_price(text):
    if not text:
        return None
    t = str(text).replace(",", "")
    m = PRICE_RE.search(t)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    if v <= 0 or v > 200000:
        return None
    return v


def _clean_title(t):
    if not t:
        return ""
    t = re.sub(r"\s+", " ", t).strip()
    return t[:180]


def _item_key(url, title):
    m = re.search(r"(?:[?&](?:id|goods_id|itemId|sku)=)(\d+)", url or "")
    if m:
        return "id:" + m.group(1)
    m = re.search(r"/(\d{6,})\.html", url or "")
    if m:
        return "id:" + m.group(1)
    return "h:" + hashlib.md5(((title or "") + (url or "")).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 页面内提取脚本
# 传入 item_url_match（商品链接域名白名单），先找符合域名的链接，再解析该链接所属卡片。
# 这样即使卡片里混着广告链接也不会抓错。
# ---------------------------------------------------------------------------
EXTRACT_JS = r"""
(cfg) => {
  const out = [];
  const seen = new Set();
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const matchesItem = h => cfg.urlMatch.some(m => (h || '').indexOf(m) >= 0);

  // 顺着父节点往上找"卡片"容器：要求内部同时有商品链接 + 像样的文字
  const findCard = (a) => {
    let el = a, depth = 0;
    let best = null;
    while (el && depth < 8) {
      if (el.querySelectorAll) {
        const links = Array.from(el.querySelectorAll('a[href]')).filter(x => matchesItem(x.href));
        const text = clean(el.innerText || '');
        // 卡片里应该只有这一件商品的链接，且文字长度合理
        if (links.length <= 2 && text.length > 8 && text.length < 900) best = el;
        if (links.length > 1) break;
      }
      el = el.parentElement; depth++;
    }
    return best || a.parentElement;
  };

  // 价格：先按选择器找，再退化到卡片文本里的 ¥ 数字
  const readPrice = (card) => {
    for (const s of cfg.price) {
      try {
        const e = card.querySelector(s);
        if (!e) continue;
        const t = clean(e.innerText);
        if (/\d/.test(t)) return t;
      } catch (e) {}
    }
    const txt = clean(card.innerText || '');
    // 淘宝会显示"¥ 3099 .05"，中间有空格
    const m = txt.match(/[¥￥]\s*([\d,]+)(?:\s*[.,]\s*(\d{1,2}))?/);
    if (m) return m[2] ? (m[1] + '.' + m[2]) : m[1];
    return '';
  };

  const readText = (card, sels, fallbackOk) => {
    for (const s of sels) {
      try {
        const e = card.querySelector(s);
        if (e) {
          const t = clean(e.innerText || e.getAttribute('title') || '');
          if (t && t.length >= (fallbackOk ? 2 : 5)) return t;
        }
      } catch (e) {}
    }
    return '';
  };

  // 主打策略：遍历所有商品链接（而不是遍历卡片），天然避开广告位
  const anchors = Array.from(document.querySelectorAll('a[href]'))
    .filter(a => matchesItem(a.href) && !seen.has(a.href));

  for (const a of anchors) {
    if (seen.has(a.href)) continue;
    seen.add(a.href);
    const card = findCard(a);
    if (!card) continue;

    // 标题：淘宝/京东都把完整标题放在 title 属性里，这是最干净的一手
    let title = '';
    for (const s of cfg.title) {
      try {
        const e = card.querySelector(s);
        if (e) {
          const t = clean(e.getAttribute('title') || e.innerText || '');
          if (t.length >= 6 && !/[¥￥]/.test(t)) { title = t; break; }
        }
      } catch (e) {}
    }
    if (!title) title = clean(a.getAttribute('title') || '');
    if (!title) {
      // 最后退化：卡片里最长的、不含价格符号的一段文字
      const cand = Array.from(card.querySelectorAll('span,div,p,a'))
        .map(e => clean(e.innerText || ''))
        .filter(t => t.length > 6
                 && !/[¥￥]/.test(t)
                 && !/人付款|人已买|已售|销量|包邮|退货宝|官方立减|补贴后|优惠后/.test(t)
                 && !/^[\d.,\s\-\/+%]+$/.test(t));
      cand.sort((x, y) => y.length - x.length);
      title = cand[0] || '';
    }
    // 兜底清理：砍掉混进来的价格尾巴和规格标签
    title = title.replace(/[¥￥]\s*[\d,.]+.*$/, '').trim();
    title = title.replace(/\s+(3年|5年|1年|全新|二手|否|是|国行|港版)\s*$/, '').trim();
    if (!title || title.length < 6) continue;

    const price = readPrice(card);
    const shop = readText(card, cfg.shop, true);

    out.push({
      url: a.href,
      title,
      price,
      shop,
      // 把卡片整段文本也带回来，后面做二次校验（防止把别的商品信息混进来）
      cardText: clean(card.innerText || '').slice(0, 400),
    });
    if (out.length >= cfg.maxItems) break;
  }

  return out;
}
"""


PDD_EXTRACT_JS = r"""
(cfg) => {
  const out = [];
  const seen = new Set();
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const lo = cfg.lo, hi = cfg.hi;
  const MAXLEN = 260;   // 卡片文本上限，放宽一点（含促销词）

  // PDD 新版卡片：没有 <a>，用 div + 每次发版就变的 hash class。
  // 判据：这个元素里恰好一个 ¥ 价格 + 有图 + 文本像商品标题。
  const divs = Array.from(document.querySelectorAll('div'));
  const cands = [];
  for (const d of divs) {
    const t = (d.innerText || '');
    if (t.length < 12 || t.length > MAXLEN) continue;
    const yen = (t.match(/[¥￥]/g) || []).length;
    if (yen !== 1) continue;
    if (!d.querySelector('img')) continue;
    cands.push({ el: d, text: clean(t), len: t.length });
  }

  // 去重：父子元素常常同时入选（同一张卡片被包了多层）。
  // 不再用"有内层就跳过"（那会只剩 1 条），改为：
  // 按文本去重，同一个文本只保留**最短**的那个容器（最贴近卡片本身）。
  const byText = new Map();
  for (const c of cands) {
    const prev = byText.get(c.text);
    if (!prev || c.len < prev.len) byText.set(c.text, c);
  }

  for (const c of byText.values()) {
    const text = c.text;

    const pm = text.match(/[¥￥]\s*(\d+(?:\.\d+)?)/);
    if (!pm) continue;
    const price = parseFloat(pm[1]);
    if (!(price >= lo && price <= hi)) continue;

    // 标题清洗：按噪声词表逐段剔除。PDD 卡片文本顺序通常是
    //   标题 + [促销/保障词] + ¥价格 + [销量/库存]
    // 所以先切掉价格及其之后的部分（那之后基本是销量/库存），
    // 再把上半段里的促销词抹掉。
    let head = text.split(/[¥￥]/)[0];                 // 价格之前的部分
    head = head
      .replace(/券后|已拼[^ ]*|本店[^ ]*|已抢[^ ]*|已售[^ ]*|月销[^ ]*|销量[^ ]*|好评[^ ]*|回头客[^ ]*|正品险|限[0-9]*件|仅剩[^ ]*|剩[^ ]*件|退货包运费|正品发票|只换不修|即将售罄|品牌热销[^ ]*|立减[^ ]*|满减[^ ]*|百亿补贴|品牌补贴|官方立减|限时[^ ]*|秒杀|新客[^ ]*|多件优惠|包邮|顺丰包邮|24小时内发货|小时发货|发货|通用[^ ]*|亲民|惠民|热销[^ ]*|[0-9]+万\\+?件/g, ' ')
      .replace(/\\s+/g, ' ').trim();

    // 标题里只剩这些泛词、没有实际型号信息 → 不是商品卡，丢掉
    if (head.length < 5) continue;
    // 必须提到内存/显卡相关词，避免把"推荐位/广告位"混进来
    if (!/ddr|内存|显卡|rtx|gddr/i.test(head)) continue;
    let title = head.slice(0, 180);

    // 链接：没 <a> 就用标题合成稳定占位（item_key 走标题 hash）
    let url = null;
    const a = c.el.querySelector('a[href]');
    if (a && a.href) url = a.href;
    if (!url) {
      const m = text.match(/(\\d{6,})/);
      url = m ? ('https://mobile.yangkeduo.com/goods.html?goods_id=' + m[1])
              : ('https://mobile.yangkeduo.com/goods.html#t=' + encodeURIComponent(title.slice(0, 40)));
    }

    const k = title.slice(0, 60);
    if (seen.has(k)) continue;
    seen.add(k);
    out.push({ url, title, price: String(price), shop: '', cardText: text });
    if (out.length >= (cfg.maxItems || 400)) break;
  }
  return out;
}
"""


def _extract_pdd(page, lo, hi):
    """拼多多专用提取：按价格卡片找，不依赖商品链接。"""
    try:
        return page.evaluate(PDD_EXTRACT_JS, {
            "lo": lo, "hi": hi,
            "maxItems": config.MAX_ITEMS_PER_KEYWORD * 2,
        }) or []
    except Exception as e:
        log("warn", f"拼多多提取脚本异常: {str(e)[:120]}")
        return []


def _extract_items(page, platform, price_range):
    cfg = config.PLATFORMS[platform]
    lo, hi = price_range

    # 拼多多改版后**页面上一个 <a> 都没有**（实测 anchors=0），商品卡片
    # 用 div + 每次发版就变的 hash class 渲染。原来"先找商品域名链接、
    # 再回溯卡片"的通用逻辑在 PDD 上必然拿到 0 条。这里走专用提取。
    if platform == "pdd":
        raw = _extract_pdd(page, lo, hi)
    else:
        try:
            raw = page.evaluate(EXTRACT_JS, {
                "price": cfg["selectors"]["price"],
                "title": cfg["selectors"]["title"],
                "shop": cfg["selectors"]["shop"],
                "urlMatch": list(cfg["item_url_match"]),
                "maxItems": config.MAX_ITEMS_PER_KEYWORD * 2,
            })
        except Exception as e:
            log("warn", f"页面提取脚本异常: {str(e)[:120]}")
            return []

    results = []
    seen = set()
    for info in raw or []:
        url = info.get("url") or ""
        title = _clean_title(info.get("title"))
        price = parse_price(info.get("price"))
        if not url or price is None:
            continue
        # PDD 的卡片没有真实链接（合成 URL 也带 goods 段，同样通过）
        if not any(m in url for m in cfg["item_url_match"]):
            continue
        if not (lo <= price <= hi):
            continue
        if len(title) < 5 or len(title) > 200:
            continue
        # 标题里塞了多个价格符号，说明抓到了整块页面而不是卡片
        if title.count("¥") + title.count("￥") > 1:
            continue
        # 卡片文本里价格出现太多次，也可能是聚合区块
        card_text = info.get("cardText") or ""
        if card_text.count("¥") + card_text.count("￥") > 4:
            continue
        key = _item_key(url, title)
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "item_key": key,
            "title": title,
            "price": price,
            "shop": _clean_title(info.get("shop"))[:60],
            "url": url,
        })
    return results


RATE_LIMIT_MARKS = (
    "访问频繁",
    "无法搜索",
    "请稍后再试",
    "操作过于频繁",
    "系统繁忙",
    "验证一下",
    "安全验证",
    "滑动验证",
    # 京东常见风控落地页
    "访问过于频繁",
    "查询过于频繁",
    "频繁访问",
    "验证码",
    "为了您的账号安全",
    "账号存在风险",
    "网络环境异常",
    "请拖动滑块",
    "拖动下方滑块",
    "人机验证",
    "安全校验",
)


def _detect_rate_limit(page, platform=None):
    """页面里出现限流文案就返回提示原话，否则 None。

    除了正文文案，还看 URL 是否被弹到京东的验证/风控中间页 ——
    京东有时不显示文案，直接 302 到验证页，此时整页也没有商品。
    URL 指纹只对京东生效，别的平台域名规则不同，不能混用。
    """
    if platform == "jd":
        try:
            cur = (page.url or "").lower()
        except Exception:
            cur = ""
        for host_mark in ("cfe.jd.com", "verify.jd.com", "risk.jd.com",
                          "pc-frequent-pro", "risk_handler"):
            if host_mark in cur:
                return f"被弹到风控验证页（{host_mark}）"
    try:
        txt = page.inner_text("body") or ""
    except Exception:
        txt = ""
    for mark in RATE_LIMIT_MARKS:
        if mark in txt:
            for line in txt.splitlines():
                if mark in line and len(line) < 80:
                    return line.strip()
            return mark
    return None


def _has_login_wall(platform, page):
    url = (page.url or "").lower()
    try:
        html = page.content()
    except Exception:
        html = ""
    if platform == "taobao":
        return "login.taobao.com" in url or "login.tmall.com" in url
    if platform == "jd":
        return "passport.jd.com" in url or "plogin.m.jd.com" in url or "login.jd.com" in url
    if platform == "pdd":
        return "login.html" in url or "/login" in url
    return False


def _scroll(page, times, pause=None, platform=None):
    """往下滚加载更多商品。温和模式下滚动更慢、中途还会停下来歇一会儿。

    platform 传入时使用该平台专属节奏（京东更慢，减少触发"访问频繁"）。
    """
    log("info", f"  滚动 {times} 次加载更多商品")
    gentle = getattr(config, "GENTLE", True)
    pace = config.pacing(platform) if platform else None
    if pace:
        base = pace["scroll_pause"]
        rest_long = pace["rest_long"]
    else:
        base = config.GENTLE_SCROLL_PAUSE if gentle else (0.7, 1.8)
        rest_long = config.GENTLE_REST_LONG
    for i in range(times):
        try:
            page.mouse.wheel(0, random.randint(700, 1500))
        except Exception:
            break
        if gentle and getattr(config, "SCROLL_REST_EVERY", 3) and (i + 1) % config.SCROLL_REST_EVERY == 0:
            page.wait_for_timeout(int(random.uniform(*rest_long) * 1000))
        else:
            page.wait_for_timeout(int((pause or random.uniform(*base)) * 1000))


# ---------------------------------------------------------------------------
# 解析失败时的现场取证
# ---------------------------------------------------------------------------
PROBE_JS = r"""
(cfg) => {
  const out = {counts: [], itemAnchors: 0, itemAnchorSamples: [], priceNodes: [], textLen: 0};
  for (const [k, list] of Object.entries(cfg.selectors)) {
    for (const q of list) {
      let n = -1;
      try { n = document.querySelectorAll(q).length; } catch (e) {}
      if (n > 0) out.counts.push([k, n, q]);
    }
  }
  const match = h => cfg.urlMatch.some(m => (h || '').indexOf(m) >= 0);
  const anchors = Array.from(document.querySelectorAll('a[href]')).filter(a => match(a.href));
  out.itemAnchors = anchors.length;
  out.itemAnchorSamples = anchors.slice(0, 8).map(a => {
    let chain = [], p = a;
    for (let i = 0; i < 4 && p && p.tagName; i++) {
      const cls = (p.className && typeof p.className === 'string') ? '.' + p.className.trim().split(' ').slice(0, 2).join('.') : '';
      chain.push(p.tagName.toLowerCase() + cls);
      p = p.parentElement;
    }
    return {href: a.href.slice(0, 100), chain: chain.join(' < ')};
  });
  out.textLen = (document.body.innerText || '').length;
  return out;
}
"""


def _diagnose(page, platform, keyword):
    """解析失败时的现场取证：记录选择器命中数、商品链接 DOM 链、整页 HTML。"""
    ts = int(time.time())
    name = f"diag_{platform}_{ts}.txt"
    path = f"{config.LOG_DIR}/{name}"
    lines = [
        f"平台: {platform}",
        f"关键词: {keyword}",
        f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    try:
        lines.append(f"URL: {page.url}")
        lines.append(f"TITLE: {page.title()}")
        body = (page.inner_text("body") or "")[:500].replace("\n", " | ")
        lines.append(f"BODY[:500]: {body}")
        cfg = config.PLATFORMS[platform]
        probe = page.evaluate(PROBE_JS, {
            "selectors": cfg["selectors"],
            "urlMatch": list(cfg["item_url_match"]),
        })
        lines.append("\n== 选择器命中数 ==")
        for k, n, q in probe.get("counts", []):
            lines.append(f"  {k:6s} {n:5d}  {q}")
        lines.append(f"\n== 符合商品域名的链接: {probe.get('itemAnchors')} 个 ==")
        for s in probe.get("itemAnchorSamples", []):
            lines.append(f"  {s['href']}")
            lines.append(f"      DOM: {s['chain']}")
        lines.append(f"\n页面文本长度: {probe.get('textLen')}")
    except Exception as e:
        lines.append(f"探针异常: {type(e).__name__}: {e}")

    try:
        html_path = f"{config.LOG_DIR}/page_{platform}_{ts}.html"
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(page.content())
        lines.append(f"\n完整 HTML: {html_path}")
    except Exception:
        html_path = None

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass
    return name


def collect(session, platform, keyword, price_range):
    """核心入口。session 由调用方在**同一线程内**创建持有。
    返回 (status, message, items)。status: ok / need_login / error
    """
    cfg = config.PLATFORMS[platform]
    name = cfg["name"]

    if not session.has_login_cookie():
        log("warn", f"[{name}] 还没有登录态，跳过")
        return "need_login", "需要登录", []

    page = session.page()
    url = cfg["search_url"].format(q=keyword.replace(" ", "%20"))
    log("info", f"[{name}] 搜索：{keyword}")
    pace = config.pacing(platform)
    try:
        # 温和模式：先进站像真人一样扫一眼首页，再点进搜索，风控明显更轻
        if getattr(config, "GENTLE", True) and not getattr(session, "home_visited", False):
            home = cfg.get("home_url")
            if home:
                try:
                    page.goto(home, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
                    page.wait_for_timeout(random.randint(*pace["home_pause"]))
                    page.mouse.wheel(0, random.randint(200, 620))
                    page.wait_for_timeout(random.randint(700, 1600))
                    session.home_visited = True
                except Exception as e:
                    log("warn", f"[{name}] 首页预热失败(不影响搜索): {str(e)[:80]}")

        page.goto(url, referer=cfg.get("home_url"), wait_until="domcontentloaded",
                  timeout=config.NAV_TIMEOUT_MS)
        page.wait_for_timeout(_rand(1800, 3200))

        if _has_login_wall(platform, page):
            log("warn", f"[{name}] 被要求登录")
            return "need_login", "需要登录", []

        # 先查限流：被限流时整页没有商品，滚多久都没用，早点退出省得越滑越黑
        hit = _detect_rate_limit(page, platform)
        if hit:
            log("warn", f"[{name}] 触发搜索限流：{hit}")
            return "rate_limited", hit, []

        _scroll(page, pace["scroll_times"], platform=platform)
        items = _extract_items(page, platform, price_range)

        if not items:
            log("info", f"[{name}] 首轮没抓到，再滚一次")
            page.wait_for_timeout(_rand(2500, 4000))
            _scroll(page, 5, platform=platform)
            items = _extract_items(page, platform, price_range)

        if not items:
            # 渲染出来几乎是空页（拼多多登录态失效时就是这样），
            # 与其报"页面改版"，不如让用户去重新登录，能修好。
            try:
                body_len = len((page.inner_text("body") or "").strip())
            except Exception:
                body_len = 0
            if body_len < 400 and platform in ("pdd", "taobao", "jd"):
                log("warn", f"[{name}] 页面几乎是空的（正文 {body_len} 字），大概率登录态失效")
                return "need_login", f"页面空渲染（正文仅 {body_len} 字），需要重新登录", []
            if _has_login_wall(platform, page):
                return "need_login", "需要登录", []
            hit = _detect_rate_limit(page, platform)
            if hit:
                log("warn", f"[{name}] 触发搜索限流：{hit}")
                return "rate_limited", hit, []
            try:
                page.screenshot(path=f"{config.LOG_DIR}/fail_{platform}_{int(time.time())}.png")
            except Exception:
                pass
            diag = _diagnose(page, platform, keyword)
            log("warn", f"[{name}] 没解析到商品，诊断见 logs/{diag}")

            # 大模型兜底：正则一条都没抽到，反正这一轮本来要失败，
            # 让本机 Ollama 尽力补抽几条（只在 items==0 时触发，正常采集零开销）。
            # 注意：这里**不能**替代主提取路径 —— 实测模型抽 40 条要 25 秒还漏 20%。
            try:
                from .. import llm_extract
                items_llm = llm_extract.extract_page_text(page, platform, price_range=(lo, hi))
                if items_llm:
                    items_llm = items_llm[: config.MAX_ITEMS_PER_KEYWORD]
                    log("info", f"[{name}] {keyword} → 模型兜底补抽 {len(items_llm)} 件")
                    return "ok", f"模型兜底 {len(items_llm)} 件", items_llm
            except Exception as e:
                log("warn", f"[{name}] 模型兜底失败(不影响主流程): {str(e)[:80]}")

            return "error", "解析不到商品（页面改版或被风控）", []

        items = items[: config.MAX_ITEMS_PER_KEYWORD]
        log("info", f"[{name}] {keyword} → {len(items)} 件")
        return "ok", f"{len(items)} 件", items
    except Exception as e:
        log("error", f"[{name}] {keyword} 异常: {str(e)[:160]}")
        return "error", str(e)[:200], []