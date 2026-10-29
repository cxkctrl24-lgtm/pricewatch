# -*- coding: utf-8 -*-
"""抓一份三平台真实搜索结果页存盘，用来调解析规则。"""
import sys
import os
import time
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pricewatch import config
from pricewatch.browser import Session

OUT = os.path.join(config.LOG_DIR, "probe")
os.makedirs(OUT, exist_ok=True)

KEYWORDS = {
    "taobao": "RTX 5070Ti",
    "jd": "RTX 5070Ti",
    "pdd": "RTX 5070Ti",
}

for platform, kw in KEYWORDS.items():
    cfg = config.PLATFORMS[platform]
    print(f"\n===== {cfg['name']} =====", flush=True)
    try:
        with Session(platform, headless=True) as s:
            print("登录态:", s.has_login_cookie(), flush=True)
            page = s.page()
            page.goto(cfg["home_url"], wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
            page.wait_for_timeout(random.randint(2000, 3200))
            url = cfg["search_url"].format(q=kw.replace(" ", "%20"))
            page.goto(url, referer=cfg["home_url"], wait_until="domcontentloaded",
                      timeout=config.NAV_TIMEOUT_MS)
            page.wait_for_timeout(random.randint(2200, 3400))
            for _ in range(4):
                page.mouse.wheel(0, random.randint(800, 1400))
                page.wait_for_timeout(random.randint(900, 1800))
            path = os.path.join(OUT, f"{platform}.html")
            open(path, "w", encoding="utf-8").write(page.content())
            print("标题:", page.title(), flush=True)
            print("最终URL:", page.url[:120], flush=True)
            print("已存:", path, os.path.getsize(path), "字节", flush=True)
    except Exception as e:
        print("失败:", type(e).__name__, str(e)[:200], flush=True)
    time.sleep(random.uniform(4, 8))