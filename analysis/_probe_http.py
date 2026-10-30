# -*- coding: utf-8 -*-
"""探针 3：核心可行性 —— 不渲染页面，纯 HTTP(带登录 cookie) 拿详情页 SSR HTML，
SKU 是否还在？对比 goto 渲染耗时。跑完可删。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("TMP", r"E:\wb\pricewatch\tmp\skuprobe")
os.environ.setdefault("TEMP", r"E:\wb\pricewatch\tmp\skuprobe")

from pricewatch.browser import Session
from pricewatch.scrapers import detail as d

ITEMS = [
    ("874277088694", "detail.tmall.com"),
    ("1057931445985", "item.taobao.com"),
    ("1082509361136", "item.taobao.com"),
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

with Session("taobao", headless=True) as s:
    print("login:", s.has_login_cookie(), flush=True)
    ctx = s.ctx

    for iid, host in ITEMS:
        url = f"https://{host}/item.htm?id={iid}"
        # A: 纯 HTTP
        t0 = time.time()
        try:
            r = ctx.request.get(url, headers={"User-Agent": UA, "Referer": "https://s.taobao.com/"},
                                timeout=30000, fail_on_status_code=False)
            html = r.text()
            props, skus = d.parse_taobao(html)
            has_base = '"skuBase"' in html
            mn = ("¥%s" % skus[0]["price"]) if skus else "-"
            print(f"[HTTP] {iid} status={r.status} {time.time()-t0:.2f}s len={len(html)//1024}KB "
                  f"skuBase={has_base} skus={len(skus)} min={mn}", flush=True)
        except Exception as e:
            print(f"[HTTP] {iid} FAIL {time.time()-t0:.2f}s {str(e)[:100]}", flush=True)
        time.sleep(4.5)

        # B: goto 渲染（对照）
        t0 = time.time()
        try:
            page = s.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
            h = page.content()
            p2, s2 = d.parse_taobao(h)
            print(f"[REND] {iid} {time.time()-t0:.2f}s len={len(h)//1024}KB skus={len(s2)}", flush=True)
            page.close()
        except Exception as e:
            print(f"[REND] {iid} FAIL {time.time()-t0:.2f}s {str(e)[:100]}", flush=True)
        time.sleep(5.0)
