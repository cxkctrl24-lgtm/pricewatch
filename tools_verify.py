# -*- coding: utf-8 -*-
"""单平台真实采集验证。用法：python tools_verify.py taobao "DDR5 16G" """
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pricewatch.browser import Session
from pricewatch.scrapers import collector

plat = sys.argv[1] if len(sys.argv) > 1 else "taobao"
kw = sys.argv[2] if len(sys.argv) > 2 else "DDR5 16G"

with Session(plat, headless=True) as s:
    logged = s.has_login_cookie()
    print(f"[{plat}] 登录态: {logged}", flush=True)
    if not logged:
        print("未登录，退出", flush=True)
    else:
        st, msg, items = collector.collect(s, plat, kw, (120, 6000))
        print(f"[{plat}] 结果: {st} | {msg} | {len(items)} 件", flush=True)
        for it in items[:12]:
            print(f'  ¥{it["price"]:>9.2f}  {it["title"][:50]}  [{it["shop"][:16]}]', flush=True)