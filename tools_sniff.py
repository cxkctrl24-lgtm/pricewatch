# -*- coding: utf-8 -*-
"""抓包：把拼多多/淘宝搜索页的网络请求全录下来，找出商品数据所在的接口。

用法：python tools_sniff.py pdd "DDR5 16G"
"""
import sys
import os
import json
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pricewatch.browser import Session

plat = sys.argv[1] if len(sys.argv) > 1 else "pdd"
kw = sys.argv[2] if len(sys.argv) > 2 else "DDR5 16G"

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "sniff")
os.makedirs(OUT, exist_ok=True)

captured = []


def on_response(resp):
    try:
        url = resp.url
        ct = (resp.headers or {}).get("content-type", "")
        if "json" not in ct.lower() and "javascript" not in ct.lower():
            return
        # 只关心可能含商品数据的接口
        if not any(k in url.lower() for k in ("search", "goods", "list", "query", "item", "api", "recommend")):
            return
        body = resp.text()
        if len(body) < 200:
            return
        captured.append({"url": url[:300], "status": resp.status, "len": len(body),
                         "ct": ct[:60], "body": body})
        print(f"  [{resp.status}] {len(body):>8}  {url[:110]}", flush=True)
    except Exception:
        pass


with Session(plat, headless=True) as s:
    print(f"[{plat}] 登录态: {s.has_login_cookie()}", flush=True)
    page = s.page()
    page.on("response", on_response)
    cfg_url = {
        "pdd": "https://mobile.yangkeduo.com/search_result.html?search_key=",
    }.get(plat)
    if plat == "pdd":
        url = cfg_url + kw.replace(" ", "%20")
    else:
        from pricewatch import config
        url = config.PLATFORMS[plat]["search_url"].format(q=kw.replace(" ", "%20"))
    print("打开:", url[:110], flush=True)
    page.goto(url, wait_until="domcontentloaded", timeout=40000)
    page.wait_for_timeout(5000)
    for _ in range(5):
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(1500)
    page.wait_for_timeout(2000)

    path = os.path.join(OUT, f"{plat}_responses.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(captured, f, ensure_ascii=False, indent=1)
    print(f"\n共捕获 {len(captured)} 个候选接口，已存 {path}", flush=True)

    # 找含 "goods" / "price" 字样的 JSON，打印结构
    for c in captured:
        b = c["body"]
        if '"goods"' in b or '"price"' in b or "goodsName" in b or "goods_id" in b:
            print(f"\n=== 疑似商品数据 {c['url'][:90]}", flush=True)
            print(b[:900], flush=True)
            break