# -*- coding: utf-8 -*-
"""拦截拼多多页面的真实搜索请求，确定 method / body / headers，用于复刻。

用法：python tools_sniff_post.py "DDR5 16G"
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pricewatch.browser import Session

kw = sys.argv[1] if len(sys.argv) > 1 else "DDR5 16G"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "sniff")
os.makedirs(OUT, exist_ok=True)

records = []


def on_request(req):
    if "/proxy/api/search" in req.url:
        r = {
            "method": req.method,
            "url": req.url,
            "post_data": req.post_data,
            "headers": {k: v for k, v in req.headers.items()
                        if k.lower() in ("content-type", "accept", "referer", "anti-content",
                                          "x-requested-with", "user-agent", "cookie-len")},
            "cookie_len": len(req.headers.get("cookie", "")),
        }
        records.append(r)
        print(f"\n=== 抓到请求 ===", flush=True)
        print("  method:", r["method"], flush=True)
        print("  url:", r["url"][:180], flush=True)
        print("  post_data:", (r["post_data"] or "")[:600], flush=True)
        print("  content-type:", r["headers"].get("content-type"), flush=True)
        print("  anti-content:", str(r["headers"].get("anti-content"))[:60], flush=True)


with Session("pdd", headless=True) as s:
    page = s.page()
    page.on("request", on_request)
    page.goto("https://mobile.yangkeduo.com/search_result.html?search_key=" + kw.replace(" ", "%20"),
              wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(6000)
    page.mouse.wheel(0, 1400)
    page.wait_for_timeout(3000)
    page.mouse.wheel(0, 1400)
    page.wait_for_timeout(3000)

with open(os.path.join(OUT, "pdd_requests.json"), "w", encoding="utf-8") as f:
    json.dump(records, f, ensure_ascii=False, indent=1)

print(f"\n共 {len(records)} 条 search 请求，已存 pdd_requests.json", flush=True)