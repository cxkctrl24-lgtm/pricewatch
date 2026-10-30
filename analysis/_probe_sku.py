# -*- coding: utf-8 -*-
"""一次性探针：验证淘宝详情页 SKU 解析路径 + 是否有更省的接口。
不属于交付物，只用来给 sku_design.md 提供实测证据。跑完可删。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pricewatch.browser import Session
from pricewatch.scrapers import detail as d

ITEM = "874277088694"  # 七彩虹 DDR4 8G16G 多代多容量（用户案例）
URL = f"https://detail.tmall.com/item.htm?id={ITEM}"


def hsize(s):
    return f"{len(s) / 1024:.0f}KB"


with Session("taobao", headless=True) as s:
    page = s.page()
    print("login cookie:", s.has_login_cookie(), flush=True)

    t0 = time.time()
    page.goto(URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    html = page.content()
    t1 = time.time()
    print(f"[PC] goto+wait {t1 - t0:.1f}s html={hsize(html)} url={page.url[:80]}", flush=True)
    print("   skuBase in html:", '"skuBase"' in html, " skuCore:", '"skuCore"' in html, flush=True)
    props, skus = d.parse_taobao(html)
    print(f"   parsed skus={len(skus)} props={len(props)}", flush=True)
    for x in skus[:20]:
        print(f"     ¥{x['price']:>8}  {x['spec']}", flush=True)
    print("   title:", page.title()[:80], flush=True)

    # ---- 更省的路径 A：detailskip sib.htm（老接口，返回 JSON）----
    time.sleep(4)
    try:
        r = s.ctx.request.get(
            "https://detailskip.taobao.com/service/getData/1/p1/item/detail/sib.htm"
            f"?itemId={ITEM}&modules=dynStock,qrcode,viewer,price,duty,xmpPromotion,delivery,upprice,activity",
            timeout=20000,
        )
        body = r.text()
        print(f"[A detailskip] status={r.status} len={len(body)}", flush=True)
        print("   head:", body[:300].replace("\n", " "), flush=True)
    except Exception as e:
        print("[A detailskip] fail:", str(e)[:150], flush=True)

    # ---- 更省的路径 B：移动端详情页（HTML 更轻？）----
    time.sleep(5)
    try:
        t0 = time.time()
        m = s.new_page()
        m.set_viewport_size({"width": 420, "height": 900})
        m.goto(f"https://h5.m.taobao.com/awp/core/detail.htm?id={ITEM}",
               wait_until="domcontentloaded", timeout=40000)
        m.wait_for_timeout(2500)
        mh = m.content()
        print(f"[B h5] {time.time() - t0:.1f}s html={hsize(mh)} url={m.url[:70]}", flush=True)
        print("   skuBase:", '"skuBase"' in mh, " skuCore:", '"skuCore"' in mh, flush=True)
        mp, ms = d.parse_taobao(mh)
        print(f"   parsed skus={len(ms)}", flush=True)
        m.close()
    except Exception as e:
        print("[B h5] fail:", str(e)[:150], flush=True)

    # ---- 更省的路径 C：搜索页卡片里是否自带 SKU ----
    time.sleep(5)
    try:
        q = s.new_page()
        q.goto("https://s.taobao.com/search?q=DDR4%20%E5%86%85%E5%AD%98%E6%9D%A1%208G",
               wait_until="domcontentloaded", timeout=45000)
        q.wait_for_timeout(2500)
        sh = q.content()
        print(f"[C search] html={hsize(sh)} has skuBase={'skuBase' in sh}", flush=True)
        for k in ("__INITIAL_DATA__", "g_page_config", "skuInfo", "nid"):
            print(f"   key {k}: {k in sh}", flush=True)
        # 抓接口
        urls = []
        q.on("request", lambda rq: urls.append(rq.url))
        q.mouse.wheel(0, 1200)
        q.wait_for_timeout(2500)
        interesting = [u for u in urls if any(k in u for k in ("h5api", "mtop", "s.taobao", "detail"))]
        print("   xhr:", json.dumps([u[:150] for u in interesting[:8]], ensure_ascii=False, indent=1), flush=True)
        q.close()
    except Exception as e:
        print("[C search] fail:", str(e)[:150], flush=True)
