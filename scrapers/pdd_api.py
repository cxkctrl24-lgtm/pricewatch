# -*- coding: utf-8 -*-
"""拼多多 JSON 接口采集（POST + anti_content 签名）。

抓包实证（tools_sniff_post.py）：
  POST https://mobile.yangkeduo.com/proxy/api/search?pdduid=<uid>
  Content-Type: application/json
  body: {"q": "<关键词>", "page": N, "size": 50, "sort": "default", "source": "search",
         "flip": "<翻页游标>", "anti_content": "<签名>", ...}
  = 所有字段都要，缺 anti_content 就返回 error_code 54001。

anti_content 是页面 JS 生成的签名，自己造不出来 —— 正确做法是在**页面上下文里**
用 hook fetch/XHR 捕获页面自己发出的那条请求，原样复刻（改 q / page / flip 即可）。

这样比解析 DOM 稳：字段全、不依赖 class 名、翻页干净。
"""
import json
import time

from ..db import log

API_MARK = "/proxy/api/search?"
MAX_WAIT = 12.0          # 等页面自己发第一条搜索请求的最长时间


def _fen(v):
    """价格字段单位是分。返回元，无效返回 None。"""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return round(n / 100.0, 2) if n > 0 else None


def _pick_price(g):
    """券后价 > 促销价 > 现价；取最低那个（对买家最实在）。"""
    cands = [
        ("券后", _fen(g.get("couponPromoPrice"))),
        ("促销", _fen(g.get("promPriceNoPlat"))),
        ("现价", _fen(g.get("price"))),
        ("成团", _fen(g.get("minOnSaleGroupPrice"))),
    ]
    vals = [(k, v) for k, v in cands if v]
    if not vals:
        return None, ""
    vals.sort(key=lambda kv: kv[1])
    return vals[0][1], vals[0][0]


def _parse_items(items, price_range):
    lo, hi = price_range
    out, seen = [], set()
    for it in items or []:
        # 三种返回形态都兼容：item_data.goods_model / item_data 本身 / it 本身
        g = it
        if isinstance(it.get("item_data"), dict):
            d = it["item_data"]
            g = d.get("goods_model") or d
        if not isinstance(g, dict):
            continue
        gid = g.get("goods_id")
        name = (g.get("goods_name") or "").strip()
        if not gid or not name:
            continue
        price, src = _pick_price(g)
        if not price or not (lo <= price <= hi):
            continue
        if gid in seen:
            continue
        seen.add(gid)

        link = g.get("link_url") or ""
        if link.startswith("goods.html"):
            url = "https://mobile.yangkeduo.com/" + link
        elif link.startswith("/"):
            url = "https://mobile.yangkeduo.com" + link
        elif link:
            url = link
        else:
            url = f"https://mobile.yangkeduo.com/goods.html?goods_id={gid}"
        url = url.split("&_oak_rcto=")[0]

        out.append({
            "item_key": "id:" + str(gid),
            "title": name[:180],
            "price": price,
            "shop": str(g.get("mall_name") or g.get("mallName") or "")[:60],
            "url": url,
            "sales": g.get("sales"),
            "price_src": src,
            "normal_price": _fen(g.get("normal_price")),
        })
    return out


CAPTURE_JS = r"""
() => {
  // 把页面自己发出的搜索请求（method/url/body/headers）截下来，供 Python 复刻
  if (window.__pddCapture) return window.__pddCapture;
  window.__pddCapture = null;
  const isSearch = (u) => u && u.indexOf('/proxy/api/search?') >= 0;

  // 1) hook fetch
  const of = window.fetch;
  window.fetch = function(input, init) {
    try {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      if (isSearch(url) && !window.__pddCapture) {
        window.__pddCapture = {kind: 'fetch', url, body: init && init.body ? String(init.body) : ''};
      }
    } catch (e) {}
    return of.apply(this, arguments);
  };

  // 2) hook XHR（有些版本走 XHR）
  const oo = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(m, u) {
    try {
      if (isSearch(u)) {
        this.addEventListener('load', () => {
          if (!window.__pddCapture) {
            window.__pddCapture = {kind: 'xhr', url: u, body: this.__pddBody || ''};
          }
        });
      }
    } catch (e) {}
    return oo.apply(this, arguments);
  };
  const os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function(b) {
    try { this.__pddBody = b ? String(b) : ''; } catch (e) {}
    return os.apply(this, arguments);
  };

  // 3) 兜底：直接找页面里能生成 anti_content 的函数
  return 'hooked';
}
"""


def _capture_request(page, keyword, timeout_s=MAX_WAIT):
    """打开搜索页并捕获一条真实搜索请求的模板。"""
    url = "https://mobile.yangkeduo.com/search_result.html?search_key=" + keyword.replace(" ", "%20")
    # hook 要在导航前生效，所以先注入一次再 goto
    try:
        page.evaluate(CAPTURE_JS)
    except Exception:
        pass
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1200)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        cap = page.evaluate("() => window.__pddCapture")
        if cap and cap.get("url"):
            return cap
        page.wait_for_timeout(600)
    return None


FETCH_JS = r"""
async (p) => {
  // 拼多多列表页/搜索页现在要求请求头带 AccessToken（值就是 PDDAccessToken
  // 这枚 cookie），否则返回空结果。页面上下文的 fetch 会自动带 cookie，
  // 但**不会**自动把 cookie 提到请求头，所以这里手动补一个。
  // 见 analysis/github_survey.md 里 onetwo1/pinduoduo 的说明。
  const headers = {
    'Content-Type': 'application/json;charset=UTF-8',
    'Accept': 'application/json',
  };
  if (p.accessToken) headers['AccessToken'] = p.accessToken;
  const r = await fetch(p.url, {
    method: 'POST',
    credentials: 'include',
    headers,
    body: p.body,
  });
  const t = await r.text();
  try { return {status: r.status, json: JSON.parse(t)}; }
  catch (e) { return {status: r.status, raw: t.slice(0, 160)}; }
}
"""


def _access_token(session):
    """从 PDD 登录态里取 AccessToken。

    实测 cookie 里是 PDDAccessToken；有的环境也可能直接叫 AccessToken。
    取不到就返回空串，由调用方按「不带这个头」继续跑（至少不会更差）。
    """
    try:
        for c in session.ctx.cookies():
            if c.get("name") in ("PDDAccessToken", "AccessToken", "pdd_access_token"):
                return c.get("value") or ""
    except Exception:
        pass
    return ""


def fetch(session, keyword, price_range, max_pages=3):
    """返回 (status, message, items)。"""
    page = session.page()
    # init_script 必须挂在 context 上，add 到已存在的 page 不生效
    try:
        session.ctx.add_init_script(CAPTURE_JS)
    except Exception:
        pass
    # 再直接注入一次，确保当前页已就位
    try:
        page.evaluate(CAPTURE_JS)
    except Exception:
        pass

    cap = _capture_request(page, keyword)
    if not cap:
        log("warn", "[拼多多] 没能捕获页面的搜索请求，退回 DOM 解析")
        return "fallback", "未捕获到接口请求", []

    # 拆出模板 body，只替换 q / page / flip
    try:
        tpl = json.loads(cap.get("body") or "{}")
    except Exception as e:
        log("warn", f"[拼多多] 请求体解析失败: {str(e)[:80]}")
        return "fallback", "请求体无法解析", []
    if not tpl.get("anti_content"):
        log("warn", "[拼多多] 请求体缺 anti_content，退回 DOM 解析")
        return "fallback", "缺签名", []

    base_url = cap["url"]
    log("info", f"[拼多多] 已捕获接口模板（anti_content {len(tpl['anti_content'])} 字符）")

    items, seen_keys = [], set()
    access_token = _access_token(session)
    if access_token:
        log("info", f"[拼多多] 已带 AccessToken（{len(access_token)} 字符）")
    else:
        log("warn", "[拼多多] 登录态里没找到 PDDAccessToken，裸请求很可能被拒")
    flip = tpl.get("flip") or ""
    for page_no in range(1, max_pages + 1):
        body = dict(tpl)
        body["q"] = keyword
        body["page"] = page_no
        if flip:
            body["flip"] = flip
        try:
            res = page.evaluate(FETCH_JS, {
                "url": base_url,
                "body": json.dumps(body, ensure_ascii=False),
                "accessToken": access_token,
            })
        except Exception as e:
            log("warn", f"[拼多多] 第 {page_no} 页请求异常: {str(e)[:100]}")
            break

        j = res.get("json") or {}
        if not j or j.get("error_code"):
            code = j.get("error_code") if j else res.get("raw", "")[:60]
            log("warn", f"[拼多多] 第 {page_no} 页被拒 error_code={code}")
            if page_no == 1:
                return "fallback", f"接口拒绝({code})", []
            break

        raw_items = j.get("items") or []
        got = _parse_items(raw_items, price_range)
        # 按 items 位置去重（翻页可能重叠）
        fresh = [x for x in got if x["item_key"] not in seen_keys]
        for x in fresh:
            seen_keys.add(x["item_key"])
        items.extend(fresh)
        log("info", f"[拼多多] 第 {page_no} 页：原始 {len(raw_items)} 条 → 可用 {len(got)}（新增 {len(fresh)}）")

        flip = j.get("flip") or ""
        if not fresh or not flip:
            break
        time.sleep(1.0 + page_no * 0.3)

    if not items:
        return "error", "接口没有可用商品", []
    return "ok", f"{len(items)} 件（接口）", items